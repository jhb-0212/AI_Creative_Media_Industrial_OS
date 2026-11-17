"""生成产出描述与登记端口契约单测（纯领域层 + 适配器，无数据库）。

覆盖三件事：
1. ``GenerationOutput`` 构造即校验——空产出不得进入资产域；
2. 确定性适配器**可复现**：同任务同 attempt 恒得同引用、同摘要；
3. 确定性资产版本号是幂等键：同任务恒定、异任务不同。
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

import pytest

from packages.errors.error_codes import DomainError
from services.generation_execution.domain.enums import GenerationTaskStatus
from services.generation_execution.domain.generation_task import GenerationTask
from services.generation_execution.domain.output import GenerationOutput
from workers.generation.adapter import (
    DeterministicGenerationAdapter,
    GenerationError,
)
from workers.generation.asset_registrar import derive_asset_version_id

_DIGEST = hashlib.sha256(b"content").hexdigest()


def _output(**overrides: Any) -> GenerationOutput:
    """构造一个合法的产出描述（测试基线）。"""
    defaults: dict[str, Any] = {
        "artifact_ref": "genresult://gen_x/attempt-1",
        "sha256": _DIGEST,
        "storage_ref": {"uri": "genresult://gen_x/attempt-1"},
        "asset_type": "VIDEO",
    }
    defaults.update(overrides)
    return GenerationOutput(**defaults)


def _task(entity_id: str | None = None, *, attempt: int = 1) -> GenerationTask:
    """构造一个合法的任务（仅用于适配器与 id 派生，不入库）。"""
    return GenerationTask(
        entity_id=entity_id or f"gen_{uuid.uuid4().hex[:12]}",
        project_id="proj_001",
        shot_id="SHOT_001",
        route_id="route_001",
        shot_spec_ref="spec_001",
        visual_plan_ref="vplan_001",
        constraint_snapshot_ref="NOT_APPLICABLE",
        budget_reservation_ref="budget_res_001",
        quota_reservation_ref="quota_res_001",
        dependency_refs=[],
        attempt=attempt,
        retry_of="gen_prev" if attempt > 1 else None,
        created_by="producer_001",
        tenant_id=uuid.uuid4(),
        status=GenerationTaskStatus.RUNNING,
    )


class TestGenerationOutputValidation:
    """产出描述的结构性约束（不合格产出不许进资产域）。"""

    def test_valid_output(self) -> None:
        assert _output().asset_type == "VIDEO"

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("artifact_ref", "  "),
            ("sha256", "not-a-digest"),
            ("sha256", _DIGEST[:63]),
            ("storage_ref", {}),
            ("asset_type", " "),
            ("asset_type", "X" * 33),
        ],
    )
    def test_invalid_fields_are_rejected(self, field: str, value: object) -> None:
        with pytest.raises(DomainError) as exc_info:
            _output(**{field: value})
        assert exc_info.value.error_code.code == "VALIDATION_ERROR"


class TestDeterministicAdapter:
    """确定性适配器：可复现、可注入失败（Phase 1 的诚实替身）。"""

    async def test_same_task_same_output(self) -> None:
        task = _task()
        adapter = DeterministicGenerationAdapter()
        first = await adapter.execute(task)
        second = await adapter.execute(task)
        assert first == second
        assert first.artifact_ref == f"genresult://{task.entity_id}/attempt-1"
        # 摘要必须由产出者给出，且与产出内容一致（不是随便一个 64 位串）。
        assert first.sha256 == hashlib.sha256(
            first.artifact_ref.encode("utf-8")
        ).hexdigest()

    async def test_digest_changes_with_attempt(self) -> None:
        """重试是新任务新 attempt：摘要不同，产出不被覆盖。"""
        base_id = f"gen_{uuid.uuid4().hex[:12]}"
        adapter = DeterministicGenerationAdapter()
        first = await adapter.execute(_task(base_id, attempt=1))
        retried = await adapter.execute(
            _task(f"{base_id}_retry", attempt=2)
        )
        assert first.sha256 != retried.sha256

    async def test_failure_injection_is_explicit(self) -> None:
        task = _task()
        adapter = DeterministicGenerationAdapter(
            fail_task_ids=frozenset({task.entity_id})
        )
        with pytest.raises(GenerationError):
            await adapter.execute(task)


class TestAssetVersionIdempotencyKey:
    """确定性资产版本号（跨域登记的幂等键）。"""

    def test_same_task_same_asset_id(self) -> None:
        task = _task()
        assert derive_asset_version_id(task) == derive_asset_version_id(task)

    def test_different_tasks_different_asset_ids(self) -> None:
        a = derive_asset_version_id(_task())
        b = derive_asset_version_id(_task())
        assert a != b

    def test_asset_id_is_readable_trace(self) -> None:
        """资产版本号自带来源：从 id 就能看出它来自哪条生成任务。"""
        task = _task()
        assert derive_asset_version_id(task) == f"av_{task.entity_id}"
