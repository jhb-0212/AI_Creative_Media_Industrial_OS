"""ADR-R47 P2：GenerationSpec 的派生、身份与执行装配。

三类判据，缺一不可：

1. **身份由内容决定**：同输入同 id / 改任一入域字段必换 id / 换租户必换 id；
2. **常规化不可漂移**：换行统一、NFC、参数白名单，任何一条被放宽都会让
   「同输入」在字节层不再唯一；
3. **策略/模板不得原地改**：摘要钉值，改内容不升版本当场报红。
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from adapters.model_gateway.fake import FakeModelGateway
from adapters.storage.local_fs import LocalFsObjectStorage
from packages.errors.error_codes import DomainError
from packages.shared.generation_template import TEMPLATE_VERSION, render_prompt, template_digest
from packages.shared.size_policy import SIZE_POLICY_VERSION, policy_digest, resolve_size
from services.production_planning.domain.generation_spec import (
    GenerationSpec,
    ShotSpecFacts,
    canonical_bytes,
    compute_spec_sha256,
    derive_spec,
)
from services.production_planning.domain.generation_task import GenerationTask
from workers.generation.executor import StorageBackedGenerationExecutor, resolve_inline_prompt

#: 策略/模板内容的钉值：改内容而不升版本会在这里报红。
_POLICY_DIGEST = "fdff27d592db07245547050ccd92e8696d6701078e02e9d1841d758324202383"
_TEMPLATE_DIGEST = "da749a3145a910226fe41af85b38d46070b17f3a8aa3806a3b12c0af3f557c50"

_TENANT = "01e4b666-b0dd-41ca-b4d4-32641f947fd9"


def _shot(**overrides) -> ShotSpecFacts:
    base = {
        "entity_id": "shot-spec-1",
        "project_id": "proj-1",
        "shot_revision_id": "rev-1",
        "aspect_ratio": "16:9",
        "camera_path": "从左后方向右前方推进",
        "subject_paths": "主角居中行进",
        "shot_size_changes": "中景转近景",
        "locked_dimensions": "camera,subject",
        "status": "LOCKED",
    }
    base.update(overrides)
    return ShotSpecFacts(**base)


def _spec(**overrides) -> GenerationSpec:
    params = {
        "shot_spec": _shot(),
        "tenant_id": _TENANT,
        "created_by": "director",
        "project_id": "proj-1",
        "production_route_ref": "route-1",
        "constraint_snapshot_id": "snap-1",
        "model_adapter_ref": "vendor/model-x",
        "provider": "vendor",
        "media_type": "image/png",
    }
    params.update(overrides)
    return derive_spec(**params)


def test_policy_and_template_contents_are_pinned() -> None:
    assert policy_digest() == _POLICY_DIGEST
    assert template_digest() == _TEMPLATE_DIGEST


def test_size_policy_is_versioned_and_fail_closed() -> None:
    assert resolve_size("16:9", policy_version=SIZE_POLICY_VERSION) == "1280x720"
    with pytest.raises(DomainError) as excinfo:
        resolve_size("7:3", policy_version=SIZE_POLICY_VERSION)
    assert "spec-size-unresolved" in str(excinfo.value)
    with pytest.raises(DomainError) as excinfo:
        resolve_size("16:9", policy_version="999")
    assert "size-policy-version-missing" in str(excinfo.value)


def test_template_omits_empty_fields_and_never_renders_nothing() -> None:
    spec = _shot(subject_paths="", shot_size_changes="")
    prompt = render_prompt(spec)
    assert "主体调度" not in prompt and "景别变化" not in prompt
    assert prompt.endswith("。")
    with pytest.raises(ValueError):
        render_prompt(
            _shot(camera_path="", subject_paths="", shot_size_changes="", aspect_ratio="")
        )


def test_same_input_yields_same_identity_and_any_change_yields_new_id() -> None:
    first = _spec()
    second = _spec()
    assert first.generation_spec_id == second.generation_spec_id
    assert first.spec_sha256 == second.spec_sha256
    assert first.generation_spec_id.startswith("gs1_")
    assert len(first.generation_spec_id) == 4 + 26
    assert first.entity_id == first.generation_spec_id

    changed = _spec(shot_spec=_shot(camera_path="从右后方向左前方推进"))
    assert changed.generation_spec_id != first.generation_spec_id
    tenant_changed = _spec(tenant_id="11111111-2222-3333-4444-555555555555")
    assert tenant_changed.generation_spec_id != first.generation_spec_id


def test_newline_normalization_and_nfc_participate_in_identity() -> None:
    base = _spec()
    payload = base.identity_payload()
    assert "\r" not in payload["prompt_text"]
    normalized = compute_spec_sha256(_TENANT, payload)
    assert normalized == base.spec_sha256


def test_canonical_bytes_have_no_whitespace_and_are_stable() -> None:
    payload = {"b": 1, "a": "中文"}
    assert canonical_bytes(payload) == b'{"a":"\xe4\xb8\xad\xe6\x96\x87","b":1}'


def test_parameters_whitelist_and_types_are_enforced() -> None:
    ok = _spec(generation_parameters={"seed": 42, "guidance_scale": "0.7"})
    assert ok.generation_parameters == {"guidance_scale": "0.7", "seed": 42}
    with pytest.raises(DomainError):
        _spec(generation_parameters={"temperature": "0.9"})
    with pytest.raises(DomainError):
        _spec(generation_parameters={"seed": 1.5})


def test_empty_negative_prompt_is_rejected_not_treated_as_absent() -> None:
    with pytest.raises(DomainError):
        _spec(negative_prompt="   ")
    plain = _spec()
    assert plain.identity_payload().get("negative_prompt") is None


def test_task_binding_equalities_are_enforced_in_domain() -> None:
    spec_id = _spec().generation_spec_id
    bound = GenerationTask(
        entity_id="task-1",
        created_by="director",
        tenant_id=_TENANT,
        project_id="proj-1",
        shot_id="shot-1",
        shot_revision_id="rev-1",
        production_route_ref="route-1",
        model_adapter_ref="vendor/model-x",
        constraint_snapshot_id="snap-1",
        budget_ref="budget-1",
        prompt_ref=f"genspec:{spec_id}",
        generation_spec_id=spec_id,
    )
    assert "prompt_ref" not in bound.to_dict()
    assert "generation_spec_id" not in bound.to_dict()
    with pytest.raises(DomainError):
        GenerationTask(
            entity_id="task-2",
            created_by="director",
            tenant_id=_TENANT,
            project_id="proj-1",
            shot_id="shot-1",
            shot_revision_id="rev-1",
            production_route_ref="route-1",
            model_adapter_ref="vendor/model-x",
            constraint_snapshot_id="snap-1",
            budget_ref="budget-1",
            prompt_ref="genspec:gs1_other",
            generation_spec_id="gs1_different",
        )
    with pytest.raises(DomainError):
        GenerationTask(
            entity_id="task-3",
            created_by="director",
            tenant_id=_TENANT,
            project_id="proj-1",
            shot_id="shot-1",
            shot_revision_id="rev-1",
            production_route_ref="route-1",
            model_adapter_ref="vendor/model-x",
            constraint_snapshot_id="snap-1",
            budget_ref="budget-1",
            prompt_ref="prompt-without-prefix",
        )


class _Registrar:
    async def prepare(self, task, **kwargs):
        return None

    async def register(self, task, artifact):
        return None

    async def list_pending(self, task):
        return []

    async def abandon(self, task, attempt_id, reason):
        return None


async def test_executor_uses_frozen_spec_fields(tmp_path: Path) -> None:
    gateway = FakeModelGateway()
    executor = StorageBackedGenerationExecutor(
        gateway=gateway,
        storage=LocalFsObjectStorage(tmp_path / "objects", max_bytes=1024 * 1024),
        resolve_prompt=resolve_inline_prompt,
        registrar=_Registrar(),
        default_model="model-default",
        media_type="image/png",
        size="1024x1024",
        resolve_input=lambda task: _resolve(task),
    )
    task = SimpleNamespace(
        entity_id="task-1",
        tenant_id=_TENANT,
        project_id="proj-1",
        shot_revision_id="rev-1",
        prompt_ref="genspec:gs1_abc",
        generation_spec_id="gs1_abc",
        attempt_count=1,
    )
    outcome = await executor.execute(task)
    assert outcome.result == "SUCCEEDED"
    assert gateway.calls[0].media_type == "video/mp4"
    assert gateway.calls[0].size == "1280x720"
    assert gateway.calls[0].model == "vendor/spec-model"


async def _resolve(task):
    return SimpleNamespace(
        prompt="派生提示词。",
        media_type="video/mp4",
        size="1280x720",
        model_id="vendor/spec-model",
        provider="vendor",
    )


def test_spec_hash_is_hex_and_id_uses_base32() -> None:
    spec = _spec()
    assert len(spec.spec_sha256) == 64
    assert spec.prompt_sha256 == hashlib.sha256(spec.prompt_text.encode("utf-8")).hexdigest()
    assert TEMPLATE_VERSION in spec.template_version
