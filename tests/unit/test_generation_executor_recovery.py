"""PR-G 崩溃恢复测试：对象在不在、内容对不对、能不能续上必须可区分。

判据不是「恢复能跑」，而是**四种情形各自被正确处理**：
- 对象存在且内容正确 → 沿用原 attempt 续 T1，**不重新调用模型、不重复落盘**；
- 对象确定不存在 → 放弃旧意图并重新生成；
- 对象存在但内容不可复用 → 放弃旧意图，绝不用损坏内容登记；
- 存储暂不可用 → **不放弃、不重生成**，报告失败留待重试（不把抖动误判成缺失）。
"""

from __future__ import annotations

import hashlib
from types import SimpleNamespace
from uuid import uuid4

import pytest

from adapters.model_gateway.fake import FakeModelGateway, deterministic_png
from adapters.storage.ports import (
    StorageIntegrityError,
    StorageObjectMissingError,
    StorageUnavailableError,
    StoredObject,
)
from packages.errors.error_codes import DomainError
from packages.shared.storage_locator import build_locator
from services.production_planning.domain.artifact_manifest import ArtifactManifest
from services.project_planning.domain.enums import ArtifactManifestStatus
from workers.generation.executor import (
    GeneratedArtifact,
    PreparedIntent,
    StorageBackedGenerationExecutor,
    resolve_inline_prompt,
)

_BYTES = deterministic_png("recover-seed")
_DIGEST = hashlib.sha256(_BYTES).hexdigest()
_LOCATOR = build_locator(_DIGEST, "png")


class _FakeStorage:
    """可注入 get 故障的最小存储端口（put 只记录，不做校验）。"""

    def __init__(
        self,
        objects: dict[str, bytes] | None = None,
        *,
        get_error: Exception | None = None,
    ) -> None:
        self._objects = dict(objects or {})
        self._get_error = get_error
        self.puts: list[str] = []

    async def put(self, *, data: bytes, media_type: str, expected_sha256: str = "") -> StoredObject:
        digest = hashlib.sha256(data).hexdigest()
        locator = build_locator(digest, "png" if media_type == "image/png" else "bin")
        self._objects[locator] = data
        self.puts.append(locator)
        return StoredObject(
            locator=locator, sha256=digest, size_bytes=len(data), media_type=media_type
        )

    async def get(self, locator: str) -> bytes:
        if self._get_error is not None:
            raise self._get_error
        if locator not in self._objects:
            raise StorageObjectMissingError(locator)
        return self._objects[locator]

    async def exists(self, locator: str) -> bool:
        return locator in self._objects


class _FakeRegistrar:
    def __init__(self, pending: list[PreparedIntent] | None = None) -> None:
        self.pending = list(pending or [])
        self.prepared: list[str] = []
        self.registered: list[GeneratedArtifact] = []
        self.abandoned: list[tuple[str, str]] = []

    async def prepare(
        self, task, *, locator: str, sha256: str, media_type: str, attempt_id: str
    ) -> None:
        self.prepared.append(locator)

    async def register(self, task, artifact: GeneratedArtifact) -> None:
        self.registered.append(artifact)

    async def list_pending(self, task) -> list[PreparedIntent]:
        return list(self.pending)

    async def abandon(self, task, attempt_id: str, reason: str) -> None:
        self.abandoned.append((attempt_id, reason))


def _task() -> SimpleNamespace:
    return SimpleNamespace(
        entity_id="gen-task-1",
        prompt_ref="inline:a lighthouse at dusk",
        model_adapter_ref="model-x",
        project_id="project-1",
        tenant_id="tenant-1",
        attempt_count=2,
        lease_owner="worker-1",
    )


def _executor(storage, registrar, gateway=None) -> StorageBackedGenerationExecutor:
    return StorageBackedGenerationExecutor(
        gateway=gateway or FakeModelGateway(),
        storage=storage,
        resolve_prompt=resolve_inline_prompt,
        registrar=registrar,
        default_model="model-default",
        media_type="image/png",
    )


def _intent(attempt_id: str = "a1") -> PreparedIntent:
    return PreparedIntent(
        attempt_id=attempt_id, locator=_LOCATOR, sha256=_DIGEST, media_type="image/png"
    )


async def test_recovery_reuses_existing_object_without_model_call() -> None:
    """对象已落盘且内容正确：沿用原 attempt 续 T1，绝不重新调用模型/落盘。"""
    storage = _FakeStorage({_LOCATOR: _BYTES})
    registrar = _FakeRegistrar([_intent()])
    gateway = FakeModelGateway()
    executor = _executor(storage, registrar, gateway)

    outcome = await executor.execute(_task())

    assert outcome.result == "SUCCEEDED"
    assert "recovered" in outcome.evidence_ref
    assert gateway.calls == []  # 未重复调用模型
    assert storage.puts == []  # 未重复落盘
    assert [a.attempt_id for a in registrar.registered] == ["a1"]
    assert registrar.prepared == []  # 没有创建新意图
    assert registrar.abandoned == []


async def test_recovery_abandons_missing_object_and_regenerates() -> None:
    """对象确定不存在：放弃旧意图，正常重新生成。"""
    storage = _FakeStorage()  # 空
    registrar = _FakeRegistrar([_intent()])
    gateway = FakeModelGateway()
    executor = _executor(storage, registrar, gateway)

    outcome = await executor.execute(_task())

    assert outcome.result == "SUCCEEDED"
    assert registrar.abandoned == [("a1", "object-absent")]
    assert len(gateway.calls) == 1  # 重新调用模型
    assert len(registrar.prepared) == 1  # 新建意图
    assert len(registrar.registered) == 1


async def test_recovery_abandons_corrupt_object_and_regenerates() -> None:
    """对象存在但内容不可复用：放弃并重新生成，不用损坏内容登记。"""
    storage = _FakeStorage(get_error=StorageIntegrityError("hash mismatch"))
    registrar = _FakeRegistrar([_intent()])
    gateway = FakeModelGateway()
    executor = _executor(storage, registrar, gateway)

    outcome = await executor.execute(_task())

    assert outcome.result == "SUCCEEDED"
    assert registrar.abandoned == [("a1", "content-integrity")]
    assert len(gateway.calls) == 1


async def test_recovery_preserves_intent_when_storage_unavailable() -> None:
    """存储暂不可用：不放弃、不重生成，报告失败留待重试。"""
    storage = _FakeStorage(get_error=StorageUnavailableError("timeout"))
    registrar = _FakeRegistrar([_intent()])
    gateway = FakeModelGateway()
    executor = _executor(storage, registrar, gateway)

    outcome = await executor.execute(_task())

    assert outcome.result == "FAILED"
    assert "storage-undetermined" in outcome.evidence_ref
    assert registrar.abandoned == []  # 意图被保留
    assert gateway.calls == []  # 没有盲目重生成
    assert storage.puts == []


async def test_no_pending_intent_falls_through_to_generation() -> None:
    """没有历史意图：直接走正常生成（恢复不制造额外副作用）。"""
    storage = _FakeStorage()
    registrar = _FakeRegistrar([])
    gateway = FakeModelGateway()
    executor = _executor(storage, registrar, gateway)

    outcome = await executor.execute(_task())

    assert outcome.result == "SUCCEEDED"
    assert registrar.abandoned == []
    assert len(gateway.calls) == 1


# ---- 状态机：ABANDONED 只能从 PREPARED 进入，且是冻结终态 -------------- #


def _manifest() -> ArtifactManifest:
    return ArtifactManifest(
        entity_id="manifest-1",
        created_by="worker-1",
        tenant_id=uuid4(),
        generation_task_id="gen-task-1",
        project_id="project-1",
        locator=_LOCATOR,
        sha256=_DIGEST,
        media_type="image/png",
        attempt_id="a1",
    )


def test_manifest_abandon_from_prepared_succeeds() -> None:
    manifest = _manifest()
    manifest.abandon("t1-abandon:object-absent")
    assert manifest.status is ArtifactManifestStatus.ABANDONED


def test_manifest_abandoned_is_frozen_terminal() -> None:
    manifest = _manifest()
    manifest.abandon("t1-abandon:object-absent")
    with pytest.raises(DomainError):
        manifest.abandon("again")


def test_manifest_stored_verified_cannot_be_abandoned() -> None:
    """已确认落盘的产物不能被改写成放弃——否则真实对象会变成无人认领的孤儿。"""
    manifest = _manifest()
    manifest.mark_stored("t1-store")
    with pytest.raises(DomainError):
        manifest.abandon("too-late")


def test_manifest_requires_media_type() -> None:
    with pytest.raises(DomainError):
        ArtifactManifest(
            entity_id="manifest-2",
            created_by="worker-1",
            tenant_id=uuid4(),
            generation_task_id="gen-task-1",
            project_id="project-1",
            locator=_LOCATOR,
            sha256=_DIGEST,
            media_type="",
            attempt_id="a1",
        )
