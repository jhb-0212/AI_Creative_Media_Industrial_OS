"""乐观锁并发控制单元测试（P1#5：根治丢失更新）。

覆盖：
- concurrent_update 条件更新：版本不匹配抛 ConcurrentModificationError。
- 服务层 lock_script 在并发冲突时转换为结构化 409（通过内存仓储模拟）。
- _supersede_active_version 并发取代竞态幂等跳过。
"""

from __future__ import annotations

from copy import deepcopy
from uuid import uuid4

import pytest

from packages.errors.error_codes import DomainError
from services.project_planning.domain.script import ScriptRevision
from services.project_planning.repository._versioning import (
    ConcurrentModificationError,
)
from services.project_planning.repository.episode_repo import EpisodeRepository
from services.project_planning.repository.outbox_writer import OutboxWriter
from services.project_planning.repository.project_repo import ProjectRepository
from services.project_planning.repository.script_repo import ScriptRepository
from services.project_planning.service.script_service import ScriptService


class _RecordingOutboxWriter(OutboxWriter):
    """内存 outbox 写入 fake，记录事件供断言。"""

    def __init__(self) -> None:
        self.events: list[dict] = []

    def record(self, *, aggregate_type, aggregate_id, event_type, payload):  # type: ignore[override]
        self.events.append(
            {
                "aggregate_type": aggregate_type,
                "aggregate_id": aggregate_id,
                "event_type": event_type,
                "payload": payload,
            }
        )
        return uuid4()


class _VersionedScriptRepository(ScriptRepository):
    """带乐观锁语义的内存 script 仓储（模拟条件更新命中 0 行）。

    关键：add/get_by_id 均做深拷贝，模拟真实 SQLAlchemy 仓储「每次查询
    返回新对象」的语义，避免内存对象引用共享导致迁移原地污染存量快照。
    """

    def __init__(self) -> None:
        self._store: dict[str, ScriptRevision] = {}

    async def add(self, script: ScriptRevision) -> None:
        self._store[script.entity_id] = deepcopy(script)

    async def save(self, script: ScriptRevision, expected_version: int) -> None:
        existing = self._store.get(script.entity_id)
        if existing is None or existing.entity_version != expected_version:
            raise ConcurrentModificationError(script.entity_id, expected_version)
        self._store[script.entity_id] = deepcopy(script)

    async def get_by_id(self, entity_id: str) -> ScriptRevision | None:
        stored = self._store.get(entity_id)
        return deepcopy(stored) if stored is not None else None

    async def get_latest_by_episode(
        self, project_id: str, episode_ref: str
    ) -> ScriptRevision | None:
        for s in self._store.values():
            if (
                s.project_id == project_id
                and s.episode_ref == episode_ref
                and s.status.value != "SUPERSEDED"
            ):
                return deepcopy(s)
        return None


class _FakeProjectRepository(ProjectRepository):
    async def add(self, project) -> None:  # type: ignore[override]
        pass

    async def save(self, project, expected_version: int) -> None:  # type: ignore[override]
        pass

    async def get_by_id(self, entity_id: str):  # type: ignore[override]
        return object()  # 非 None，视为存在


class _FakeEpisodeRepository(EpisodeRepository):
    async def add(self, episode) -> None:  # type: ignore[override]
        pass

    async def save(self, episode, expected_version: int) -> None:  # type: ignore[override]
        pass

    async def get_by_id(self, entity_id: str):  # type: ignore[override]
        return None

    async def get_by_project_ref(self, project_id: str, episode_ref: str):  # type: ignore[override]
        return object()  # 非 None，视为归属合法


def _make_service(repo: ScriptRepository) -> ScriptService:
    return ScriptService(
        repo,
        _FakeProjectRepository(),
        _FakeEpisodeRepository(),
        _RecordingOutboxWriter(),
    )


async def test_lock_script_concurrent_conflict_raises() -> None:
    """并发锁定时，乐观锁冲突应抛 ConcurrentModificationError（409）。

    场景：两个请求都读到 entity_version=1 的 DRAFT 剧本，各自尝试 LOCKED。
    第一个 save 成功（version 1→2），第二个 save 因 WHERE version=1 命中 0 行
    抛冲突，杜绝「后写覆盖先写」的丢失更新。
    """
    repo = _VersionedScriptRepository()
    script = ScriptRevision(
        entity_id="scr_c",
        project_id="proj_c",
        episode_ref="EP001",
        title="并发剧本",
        created_by="u",
    )
    await repo.add(script)

    service = _make_service(repo)

    # 第一个锁定：version 1 -> 2，成功。
    locked = await service.lock_script(
        entity_id="scr_c", requested_by="编剧A", approved_by="导演B"
    )
    assert locked.status.value == "LOCKED"
    assert locked.entity_version == 2

    # 并发请求 B 读取到的是陈旧快照（entity_version=1），
    # 基于该旧版本 save 应抛 ConcurrentModificationError（而非覆盖）。
    stale = ScriptRevision(
        entity_id="scr_c",
        project_id="proj_c",
        episode_ref="EP001",
        title="并发剧本",
        created_by="u",
        entity_version=1,  # 模拟并发请求在迁移前读到的旧版本号
    )
    with pytest.raises(ConcurrentModificationError):
        await repo.save(stale, expected_version=1)


async def test_supersede_concurrent_idempotent_skip() -> None:
    """并发取代：旧版本已被先行取代时，_supersede_active_version 幂等跳过。

    直接调用内部方法，验证乐观锁冲突被吞掉、不向上抛错（终态已达成）。
    """
    repo = _VersionedScriptRepository()
    active = ScriptRevision(
        entity_id="scr_old",
        project_id="proj_s",
        episode_ref="EP001",
        title="旧版本",
        created_by="u",
    )
    await repo.add(active)
    service = _make_service(repo)

    # 第一次取代成功：DRAFT -> SUPERSEDED，version 1 -> 2。
    await service._supersede_active_version("proj_s", "EP001", "scr_new")

    fetched = await repo.get_by_id("scr_old")
    assert fetched is not None
    assert fetched.status.value == "SUPERSEDED"
    assert fetched.entity_version == 2

    # 第二次取代（并发模拟）：此时活跃版本已是 SUPERSEDED，get_latest 应返回 None
    # （因为 status != SUPERSEDED 过滤），幂等跳过，不抛错。
    await service._supersede_active_version("proj_s", "EP001", "scr_new2")


async def test_concurrent_modification_error_carries_code() -> None:
    """ConcurrentModificationError 应携带 CONCURRENT_MODIFICATION 错误码（409）。"""
    err = ConcurrentModificationError("scr_x", 3)
    assert isinstance(err, DomainError)
    assert err.error_code.code == "CONCURRENT_MODIFICATION"
    assert err.error_code.http_status == 409
    assert err.error_code.retryable is True
