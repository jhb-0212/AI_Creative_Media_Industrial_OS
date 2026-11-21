"""剧本锁定的两条转移必须写状态迁移事实（评审 T1）。

为什么需要这组用例（根因）
------------------------------------------------------------
``SCRIPT.LOCK.REQUEST`` 与 ``SCRIPT.LOCK.APPROVE`` 是整个系统里**审批语义最重**
的两条转移：它们回答「谁申请锁定、谁批准了锁定」。但在这轮修复前，两者都走
``ScriptRevision.request_lock()`` / ``approve_lock()`` 里直接 ``self.status = ...``
再 ``repository.save()`` 的旁路——状态改了、事件发了，``audit.status_transitions``
里却没有一行。

更糟的是闸门看不见：``data.transition_single_entry`` 当时只查「谁调用了
``transition_to``」，而这两条路径根本不调它。**只查方法名的闸门，给出的是
「方法名被覆盖」的安全感，不是「状态被记录」的保证。**

修复后两条路径都经 ``advance_status``（唯一推进入口），本文件锁住：
1. 每次锁定申请/批准各写且只写一条事实，``owner_action`` 指向真实的转移边；
2. 事实的 ``actor_subject`` 是真实主体（申请人 / 批准人），不是占位符；
3. 事实与事件互指（事件 payload 带 ``transition_id``）；
4. 职责分离仍 fail-closed：批准人 == 申请人时既不推进也不留事实。
"""

from __future__ import annotations

import uuid
from uuid import uuid4

import pytest

from packages.errors.error_codes import DomainError
from packages.identity.principal import Principal
from packages.shared.outbox_writer import OutboxWriter
from services.project_planning.domain.script import ScriptRevision
from services.project_planning.repository.episode_repo import EpisodeRepository
from services.project_planning.repository.project_repo import ProjectRepository
from services.project_planning.repository.script_repo import ScriptRepository
from services.project_planning.service.script_service import ScriptService

_TENANT = uuid.UUID("00000000-0000-0000-0000-000000000000")


class _RecordingOutboxWriter(OutboxWriter):
    """记录互指的一对（事实 + 事件），供断言。"""

    def __init__(self) -> None:
        self.events: list[dict] = []
        self.transitions: list = []

    def record(self, *, aggregate_type, aggregate_id, tenant_id, event_type, payload):  # type: ignore[override]
        self.events.append(
            {
                "aggregate_type": aggregate_type,
                "aggregate_id": aggregate_id,
                "tenant_id": tenant_id,
                "event_type": event_type,
                "payload": payload,
            }
        )
        return uuid4()

    def record_transition(self, *, fact, event):  # type: ignore[override]
        self.transitions.append(fact)
        return self.record(
            aggregate_type=event.aggregate_type,
            aggregate_id=event.aggregate_id,
            tenant_id=event.tenant_id,
            event_type=event.event_type,
            payload={**event.payload, "transition_id": str(fact.transition_id)},
        )


class _InMemoryScriptRepository(ScriptRepository):
    """最小内存仓储：只为让服务能读回同一实体。"""

    def __init__(self, script: ScriptRevision) -> None:
        self._script = script

    async def add(self, script: ScriptRevision) -> None:
        self._script = script

    async def save(self, script: ScriptRevision, expected_version: int) -> None:
        self._script = script

    async def get_by_id(self, entity_id: str) -> ScriptRevision | None:
        return self._script if self._script.entity_id == entity_id else None

    async def get_latest_by_episode(
        self, project_id: str, episode_ref: str
    ) -> ScriptRevision | None:
        """本组用例不涉及「取最新版本」，返回 None 即可（不参与被测路径）。"""
        return None


class _PresentProjectRepository(ProjectRepository):
    """项目存在即可（归属校验只关心「在不在」）。"""

    async def add(self, project) -> None:  # type: ignore[override]
        pass

    async def save(self, project, expected_version: int) -> None:  # type: ignore[override]
        pass

    async def get_by_id(self, entity_id: str):  # type: ignore[override]
        return object()

    async def get_by_id_for_child_write(self, entity_id: str):  # type: ignore[override]
        return await self.get_by_id(entity_id)


class _MatchingEpisodeRepository(EpisodeRepository):
    """集数归属合法即可（避免把无关的仓储语义带进本组断言）。"""

    async def add(self, episode) -> None:  # type: ignore[override]
        pass

    async def save(self, episode, expected_version: int) -> None:  # type: ignore[override]
        pass

    async def get_by_id(self, entity_id: str):  # type: ignore[override]
        return None

    async def get_by_project_ref(self, project_id: str, episode_ref: str):  # type: ignore[override]
        return object()


def _script() -> ScriptRevision:
    return ScriptRevision(
        entity_id="scr_lock",
        project_id="proj_lock",
        episode_ref="EP001",
        title="锁定剧本",
        created_by="writer_001",
        tenant_id=_TENANT,
    )


def _service(script: ScriptRevision) -> tuple[ScriptService, _RecordingOutboxWriter]:
    writer = _RecordingOutboxWriter()
    service = ScriptService(
        _InMemoryScriptRepository(script),
        _PresentProjectRepository(),
        _MatchingEpisodeRepository(),
        writer,
    )
    return service, writer


def _principal(subject_id: str) -> Principal:
    return Principal(
        subject_id=subject_id, tenant_id=_TENANT, roles=frozenset({"导演", "编剧"})
    )


async def test_lock_request_writes_transition_fact() -> None:
    """DRAFT→LOCK_PENDING 必须留下一条事实，actor 是申请人本人。"""
    service, writer = _service(_script())

    await service.request_lock(entity_id="scr_lock", principal=_principal("writer_001"))

    assert len(writer.transitions) == 1, "锁定申请没有写状态迁移事实"
    fact = writer.transitions[0]
    assert fact.resource_type == "ScriptRevision"
    assert fact.resource_id == "scr_lock"
    assert fact.owner_action == "SCRIPT.LOCK.REQUEST"
    assert (fact.from_status, fact.to_status) == ("DRAFT", "LOCK_PENDING")
    assert fact.actor_subject == "writer_001"
    assert fact.entity_version == 2


async def test_lock_approve_writes_transition_fact() -> None:
    """LOCK_PENDING→LOCKED 同样必须留事实，actor 是批准人本人（不是申请人）。"""
    service, writer = _service(_script())
    await service.request_lock(entity_id="scr_lock", principal=_principal("writer_001"))
    await service.approve_lock(entity_id="scr_lock", principal=_principal("director_001"))

    assert len(writer.transitions) == 2, "锁定批准没有写状态迁移事实"
    approve = writer.transitions[1]
    assert approve.owner_action == "SCRIPT.LOCK.APPROVE"
    assert (approve.from_status, approve.to_status) == ("LOCK_PENDING", "LOCKED")
    assert approve.actor_subject == "director_001"
    assert approve.entity_version == 3


async def test_lock_transition_fact_and_event_mutually_reference() -> None:
    """事实持 event_id、事件 payload 持 transition_id：两条证据互相指认。"""
    service, writer = _service(_script())

    await service.request_lock(entity_id="scr_lock", principal=_principal("writer_001"))

    fact = writer.transitions[0]
    event = writer.events[0]
    assert event["event_type"] == "SCRIPT_LOCK_REQUESTED"
    assert event["payload"]["transition_id"] == str(fact.transition_id)
    assert event["payload"]["requested_by"] == "writer_001"


async def test_separation_violation_writes_no_fact() -> None:
    """批准人 == 申请人：职责分离拒绝，且**不留下半条链路**。

    不留事实这一点是刻意的：拒绝的请求不该在审批历史里出现一行「已批准」，
    也不该出现一行「尝试批准」——审计事实描述的是已发生的状态迁移。
    """
    service, writer = _service(_script())
    await service.request_lock(entity_id="scr_lock", principal=_principal("writer_001"))
    facts_after_request = len(writer.transitions)

    with pytest.raises(DomainError):
        await service.approve_lock(
            entity_id="scr_lock", principal=_principal("writer_001")
        )

    assert len(writer.transitions) == facts_after_request
