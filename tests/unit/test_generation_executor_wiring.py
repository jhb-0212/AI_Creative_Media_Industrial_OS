"""执行器编排测试：每一段失败都必须产出可区分的证据。

判据不是「成功路径能跑」，而是**四种失败各自被识别**。把失败压成同一个
``FAILED`` 会让调度侧无法判断「值不值得再跑一次」，也让排障只能靠翻日志。

同时锁定一个语义：``prompt_ref`` 不可解析时**不得调用网关**。若在解析失败后
仍发请求，说明实现把 ref 当成了正文——那种实现会让整条链路「成功」地产出
与意图无关的内容。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from adapters.model_gateway.fake import FakeModelGateway
from adapters.storage.local_fs import LocalFsObjectStorage
from workers.generation.executor import (
    AssetRegistrationUnavailableError,
    GeneratedArtifact,
    StorageBackedGenerationExecutor,
    UnwiredAssetRegistrar,
    resolve_inline_prompt,
)


class _RecordingRegistrar:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[str, str]] = []
        self._fail = fail

    async def prepare(self, task, *, locator: str, sha256: str, attempt_id: str):
        self.calls.append(("prepare", locator))
        if self._fail:
            raise AssetRegistrationUnavailableError("模拟未接线")

    async def register(self, task, artifact: GeneratedArtifact):
        self.calls.append(("register", artifact.stored.locator))
        if self._fail:
            raise AssetRegistrationUnavailableError("模拟未接线")


def _task(**overrides) -> SimpleNamespace:
    base = {
        "entity_id": "gen-exec-1",
        "prompt_ref": "inline:a lighthouse at dusk",
        "model_adapter_ref": "model-x",
        "project_id": "project-1",
        "tenant_id": "tenant-1",
        "attempt_count": 1,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _executor(
    tmp_path: Path,
    *,
    gateway=None,
    registrar=None,
    max_bytes: int = 1024 * 1024,
) -> StorageBackedGenerationExecutor:
    return StorageBackedGenerationExecutor(
        gateway=gateway or FakeModelGateway(),
        storage=LocalFsObjectStorage(tmp_path / "objects", max_bytes=max_bytes),
        resolve_prompt=resolve_inline_prompt,
        registrar=registrar or _RecordingRegistrar(),
        default_model="model-default",
        media_type="image/png",
    )


async def test_success_registers_asset_and_lands_bytes(tmp_path: Path) -> None:
    registrar = _RecordingRegistrar()
    executor = _executor(tmp_path, registrar=registrar)

    outcome = await executor.execute(_task())

    assert outcome.result == "SUCCEEDED"
    # 执行器不再返回资产 ID：结果对象根本没有该字段（C7 单一入队事实源）。
    assert not hasattr(outcome, "asset_version_id")
    locator = outcome.evidence_ref.split("|")[0]
    assert locator.startswith("local://")
    storage = LocalFsObjectStorage(tmp_path / "objects", max_bytes=1024 * 1024)
    assert await storage.exists(locator)
    # T0 意图先于落盘，T1 确认在落盘之后；顺序反了就失去崩溃恢复依据。
    assert [call[0] for call in registrar.calls] == ["prepare", "register"]


async def test_unresolvable_prompt_never_calls_gateway(tmp_path: Path) -> None:
    """ref 当正文的实现会在这里「成功」，因此这条用例是它的照妖镜。"""
    gateway = FakeModelGateway()
    executor = _executor(tmp_path, gateway=gateway)

    outcome = await executor.execute(_task(prompt_ref="prompt-8f3a2b"))

    assert outcome.result == "FAILED"
    assert outcome.evidence_ref.startswith("prompt-unresolvable")
    assert gateway.calls == []


@pytest.mark.parametrize(
    ("mode", "prefix"),
    [
        ("timeout", "gateway-retryable"),
        ("server_error", "gateway-retryable"),
        ("rejected", "gateway-rejected"),
        ("misconfigured", "gateway-misconfigured"),
    ],
)
async def test_gateway_failures_are_classified(
    tmp_path: Path, mode: str, prefix: str
) -> None:
    executor = _executor(tmp_path, gateway=FakeModelGateway(fail_mode=mode))

    outcome = await executor.execute(_task())

    assert outcome.result == "FAILED"
    assert outcome.evidence_ref.startswith(prefix)


async def test_storage_limit_failure_is_reported_as_storage(tmp_path: Path) -> None:
    executor = _executor(tmp_path, max_bytes=64)  # 产物 4096 字节，必然超限

    outcome = await executor.execute(_task())

    assert outcome.result == "FAILED"
    assert outcome.evidence_ref.startswith("storage-")


async def test_unwired_registrar_fails_closed_before_bytes_land(tmp_path: Path) -> None:
    """T0 意图失败就不落盘：否则留下无法恢复的孤儿对象。"""
    executor = _executor(tmp_path, registrar=UnwiredAssetRegistrar())

    outcome = await executor.execute(_task())

    assert outcome.result == "FAILED"
    assert outcome.evidence_ref.startswith("artifact-intent-")
    assert not any((tmp_path / "objects").rglob("*.png"))


async def test_same_task_twice_yields_same_locator(tmp_path: Path) -> None:
    """内容寻址的幂等性：重跑同一任务不会产生第二份产物。"""
    executor = _executor(tmp_path)
    first = await executor.execute(_task())
    second = await executor.execute(_task())

    assert first.evidence_ref.split("|")[0] == second.evidence_ref.split("|")[0]
    assert len(list((tmp_path / "objects").rglob("*.png"))) == 1


async def test_declared_digest_mismatch_is_caught_as_storage_failure(
    tmp_path: Path,
) -> None:
    """网关声明的摘要与实际字节不符 ⇒ 不落盘、不宣称成功。"""

    class _LyingGateway:
        async def generate(self, request):
            from adapters.model_gateway.ports import GatewayArtifact

            return GatewayArtifact(
                data=b"\x89PNG\r\n\x1a\nlie",
                media_type="image/png",
                declared_sha256="0" * 64,
            )

    executor = _executor(tmp_path, gateway=_LyingGateway())
    outcome = await executor.execute(_task())

    assert outcome.result == "FAILED"
    assert outcome.evidence_ref.startswith("gateway-digest-mismatch")
    assert not any((tmp_path / "objects").rglob("*.png"))


async def test_evidence_ref_is_bounded(tmp_path: Path) -> None:
    """证据串有长度上限：超长会撑爆列宽或被截断成不可读的残句。"""
    executor = _executor(
        tmp_path, gateway=FakeModelGateway(fail_mode="rejected")
    )
    outcome = await executor.execute(_task())
    assert len(outcome.evidence_ref) <= 200


async def test_inline_prefix_without_body_is_rejected() -> None:
    with pytest.raises(Exception, match="无法解析"):
        await resolve_inline_prompt("inline:   ")
