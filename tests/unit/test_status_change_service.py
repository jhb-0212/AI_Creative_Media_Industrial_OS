"""Project/Episode 状态变更（STATUS.CHANGE）用例的单元测试（Phase 6 / F01）。

验证：
- 合法转移：目标状态作参数，按状态机转移表执行（DRAFT→ACTIVE 等）。
- 非法转移：未登记转移一律拒绝（STATE_TRANSITION_DENIED）。
- 终态不可原地复活（ARCHIVED → 任意状态拒绝）。
- 乐观锁：并发修改抛 CONCURRENT_MODIFICATION。
- 状态变更与审计事件同事务记录（PROJECT_STATUS_CHANGED / EPISODE_STATUS_CHANGED）。
- 对象不存在抛 NOT_FOUND；changed_by 空值拒绝。
"""

from __future__ import annotations

import uuid
from uuid import uuid4

import pytest

from packages.errors.error_codes import DomainError
from packages.shared.outbox_writer import OutboxWriter
from services.project_planning.domain.enums import EpisodeStatus, ProjectStatus, ProjectTier
from services.project_planning.domain.episode import Episode
from services.project_planning.domain.project import Project
from services.project_planning.repository._versioning import ConcurrentModificationError
from services.project_planning.repository.episode_repo import EpisodeRepository
from services.project_planning.repository.project_repo import ProjectRepository
from services.project_planning.service.episode_service import EpisodeService
from services.project_planning.service.project_service import ProjectService

_TENANT = uuid.UUID("00000000-0000-0000-0000-000000000000")


class _RecordingOutboxWriter(OutboxWriter):
    """内存 outbox 写入 fake，用于断言状态变更事件。"""

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
        """写入互指的一对：事件（带 transition_id）+ 事实（测试侧只留存待断言）。"""
        self.transitions.append(fact)
        return self.record(
            aggregate_type=event.aggregate_type,
            aggregate_id=event.aggregate_id,
            tenant_id=event.tenant_id,
            event_type=event.event_type,
            payload={**event.payload, "transition_id": str(fact.transition_id)},
        )


def _project(entity_id: str = "proj_001", status: ProjectStatus = ProjectStatus.DRAFT) -> Project:
    return Project(
        entity_id=entity_id,
        project_name="测试项目",
        project_tier=ProjectTier.B,
        created_by="user_001",
        tenant_id=_TENANT,
        status=status,
    )


def _episode(
    entity_id: str = "ep_001", status: EpisodeStatus = EpisodeStatus.DRAFT
) -> Episode:
    return Episode(
        entity_id=entity_id,
        project_id="proj_001",
        episode_ref="EP001",
        title="测试集数",
        created_by="user_001",
        tenant_id=_TENANT,
        status=status,
    )


class _FakeProjectRepository(ProjectRepository):
    """内存 project 仓储，支持 save 时按乐观锁语义模拟并发冲突。"""

    def __init__(self, seed: Project | None = None, *, fail_on_save: bool = False) -> None:
        self._store: dict[str, Project] = {}
        if seed is not None:
            self._store[seed.entity_id] = seed
        self._fail_on_save = fail_on_save

    async def add(self, project: Project) -> None:
        self._store[project.entity_id] = project

    async def save(self, project: Project, expected_version: int) -> None:
        if self._fail_on_save:
            raise ConcurrentModificationError(project.entity_id, expected_version)
        self._store[project.entity_id] = project

    async def get_by_id(self, entity_id: str) -> Project | None:
        return self._store.get(entity_id)

    async def get_by_id_for_child_write(self, entity_id: str) -> Project | None:
        return await self.get_by_id(entity_id)


class _FakeEpisodeRepository(EpisodeRepository):
    """内存 episode 仓储，支持 save 时模拟并发冲突。"""

    def __init__(self, seed: Episode | None = None, *, fail_on_save: bool = False) -> None:
        self._store: dict[str, Episode] = {}
        if seed is not None:
            self._store[seed.entity_id] = seed
        self._fail_on_save = fail_on_save

    async def add(self, episode: Episode) -> None:
        self._store[episode.entity_id] = episode

    async def save(self, episode: Episode, expected_version: int) -> None:
        if self._fail_on_save:
            raise ConcurrentModificationError(episode.entity_id, expected_version)
        self._store[episode.entity_id] = episode

    async def get_by_id(self, entity_id: str) -> Episode | None:
        return self._store.get(entity_id)

    async def get_by_project_ref(
        self, project_id: str, episode_ref: str
    ) -> Episode | None:
        for ep in self._store.values():
            if ep.project_id == project_id and ep.episode_ref == episode_ref:
                return ep
        return None

    async def get_by_project_ref_for_child_write(
        self, project_id: str, episode_ref: str
    ) -> Episode | None:
        return await self.get_by_project_ref(project_id, episode_ref)


# --------------------------------------------------------------------------- #
# Project.change_status
# --------------------------------------------------------------------------- #
async def test_project_change_status_legal_transition() -> None:
    """合法转移：DRAFT → ACTIVE 执行成功，状态推进且版本 +1。"""
    project = _project(status=ProjectStatus.DRAFT)
    repo = _FakeProjectRepository(seed=project)
    outbox = _RecordingOutboxWriter()
    service = ProjectService(repo, outbox)

    result = await service.change_status(
        entity_id="proj_001",
        target_status=ProjectStatus.ACTIVE,
        changed_by="user_002",
        tenant_id=_TENANT,
    )

    assert result.status == ProjectStatus.ACTIVE
    assert result.entity_version == 2
    # 事件与状态变更同事务记录，from_status 是迁移前的真实来源态。
    assert len(outbox.events) == 1
    event = outbox.events[0]
    assert event["event_type"] == "PROJECT_STATUS_CHANGED"
    assert event["payload"]["from_status"] == "DRAFT"
    assert event["payload"]["to_status"] == "ACTIVE"
    assert event["payload"]["changed_by"] == "user_002"


async def test_project_change_status_illegal_transition_denied() -> None:
    """非法转移：DRAFT → COMPLETED（未登记）拒绝，状态不变。"""
    project = _project(status=ProjectStatus.DRAFT)
    repo = _FakeProjectRepository(seed=project)
    outbox = _RecordingOutboxWriter()
    service = ProjectService(repo, outbox)

    with pytest.raises(DomainError) as exc_info:
        await service.change_status(
            entity_id="proj_001",
            target_status=ProjectStatus.COMPLETED,
            changed_by="user_002",
            tenant_id=_TENANT,
        )
    assert exc_info.value.error_code.code == "STATE_TRANSITION_DENIED"
    # 非法转移不得产生事件。
    assert outbox.events == []


async def test_project_change_status_archived_cannot_revive() -> None:
    """终态不可原地复活：ARCHIVED → ACTIVE 拒绝（红线）。"""
    project = _project(status=ProjectStatus.ARCHIVED)
    repo = _FakeProjectRepository(seed=project)
    service = ProjectService(repo, _RecordingOutboxWriter())

    with pytest.raises(DomainError) as exc_info:
        await service.change_status(
            entity_id="proj_001",
            target_status=ProjectStatus.ACTIVE,
            changed_by="user_002",
            tenant_id=_TENANT,
        )
    assert exc_info.value.error_code.code == "STATE_TRANSITION_DENIED"


async def test_project_change_status_concurrent_conflict() -> None:
    """乐观锁：save 抛并发冲突 → 服务层转 CONCURRENT_MODIFICATION。"""
    project = _project(status=ProjectStatus.DRAFT)
    repo = _FakeProjectRepository(seed=project, fail_on_save=True)
    service = ProjectService(repo, _RecordingOutboxWriter())

    with pytest.raises(DomainError) as exc_info:
        await service.change_status(
            entity_id="proj_001",
            target_status=ProjectStatus.ACTIVE,
            changed_by="user_002",
            tenant_id=_TENANT,
        )
    assert exc_info.value.error_code.code == "CONCURRENT_MODIFICATION"


async def test_project_change_status_not_found() -> None:
    """对象不存在抛 NOT_FOUND。"""
    service = ProjectService(_FakeProjectRepository(), _RecordingOutboxWriter())
    with pytest.raises(DomainError) as exc_info:
        await service.change_status(
            entity_id="proj_missing",
            target_status=ProjectStatus.ACTIVE,
            changed_by="user_002",
            tenant_id=_TENANT,
        )
    assert exc_info.value.error_code.code == "NOT_FOUND"


async def test_project_change_status_rejects_empty_changed_by() -> None:
    """changed_by 空值拒绝（VALIDATION_ERROR）。"""
    project = _project(status=ProjectStatus.DRAFT)
    service = ProjectService(_FakeProjectRepository(seed=project), _RecordingOutboxWriter())
    with pytest.raises(DomainError) as exc_info:
        await service.change_status(
            entity_id="proj_001",
            target_status=ProjectStatus.ACTIVE,
            changed_by="   ",
            tenant_id=_TENANT,
        )
    assert exc_info.value.error_code.code == "VALIDATION_ERROR"


# --------------------------------------------------------------------------- #
# Episode.change_status
# --------------------------------------------------------------------------- #
async def test_episode_change_status_legal_transition() -> None:
    """合法转移：DRAFT → ACTIVE 执行成功，事件携带 project_id。"""
    episode = _episode(status=EpisodeStatus.DRAFT)
    repo = _FakeEpisodeRepository(seed=episode)
    outbox = _RecordingOutboxWriter()
    service = EpisodeService(repo, _FakeProjectRepository(), outbox)

    result = await service.change_status(
        entity_id="ep_001",
        target_status=EpisodeStatus.ACTIVE,
        changed_by="user_002",
        tenant_id=_TENANT,
    )

    assert result.status == EpisodeStatus.ACTIVE
    assert len(outbox.events) == 1
    event = outbox.events[0]
    assert event["event_type"] == "EPISODE_STATUS_CHANGED"
    assert event["payload"]["from_status"] == "DRAFT"
    assert event["payload"]["to_status"] == "ACTIVE"
    assert event["payload"]["project_id"] == "proj_001"


async def test_episode_change_status_archived_cannot_revive() -> None:
    """终态不可原地复活：ARCHIVED → ACTIVE 拒绝。"""
    episode = _episode(status=EpisodeStatus.ARCHIVED)
    repo = _FakeEpisodeRepository(seed=episode)
    service = EpisodeService(repo, _FakeProjectRepository(), _RecordingOutboxWriter())

    with pytest.raises(DomainError) as exc_info:
        await service.change_status(
            entity_id="ep_001",
            target_status=EpisodeStatus.ACTIVE,
            changed_by="user_002",
            tenant_id=_TENANT,
        )
    assert exc_info.value.error_code.code == "STATE_TRANSITION_DENIED"


async def test_episode_change_status_concurrent_conflict() -> None:
    """乐观锁：save 抛并发冲突 → 转 CONCURRENT_MODIFICATION。"""
    episode = _episode(status=EpisodeStatus.DRAFT)
    repo = _FakeEpisodeRepository(seed=episode, fail_on_save=True)
    service = EpisodeService(repo, _FakeProjectRepository(), _RecordingOutboxWriter())

    with pytest.raises(DomainError) as exc_info:
        await service.change_status(
            entity_id="ep_001",
            target_status=EpisodeStatus.ACTIVE,
            changed_by="user_002",
            tenant_id=_TENANT,
        )
    assert exc_info.value.error_code.code == "CONCURRENT_MODIFICATION"


# --------------------------------------------------------------------------- #
# 迁移事实的归属字段（评审 T3）
# --------------------------------------------------------------------------- #
async def test_project_transition_fact_carries_organization() -> None:
    """组织项目的迁移事实必须带组织归属，**调用方不传也要带**。

    评审 T3 的事实：``advance_status`` 把 organization_id 完全交给调用方传，
    项目服务漏传一次，组织项目的历史就被记成了「无组织」——因为事实表里
    NULL 的语义是「该资源没有组织归属」，不是「忘了填」。这类错误在数据里
    看不出来（行是完整的），只在按组织查历史时才暴露，所以必须由实体自身
    派生，并用用例锁住「不传也对」。
    """
    organization = uuid4()
    project = Project(
        entity_id="proj_org",
        project_name="组织项目",
        project_tier=ProjectTier.B,
        created_by="user_001",
        tenant_id=_TENANT,
        status=ProjectStatus.DRAFT,
        organization_id=organization,
    )
    outbox = _RecordingOutboxWriter()
    service = ProjectService(_FakeProjectRepository(seed=project), outbox)

    await service.change_status(
        entity_id="proj_org",
        target_status=ProjectStatus.ACTIVE,
        changed_by="user_002",
        tenant_id=_TENANT,
    )

    assert len(outbox.transitions) == 1
    assert outbox.transitions[0].organization_id == organization


async def test_personal_project_transition_fact_has_no_organization() -> None:
    """个人租户项目：事实里组织为空，与「无组织归属」语义一致（不是漏填）。"""
    outbox = _RecordingOutboxWriter()
    service = ProjectService(_FakeProjectRepository(seed=_project()), outbox)

    await service.change_status(
        entity_id="proj_001",
        target_status=ProjectStatus.ACTIVE,
        changed_by="user_002",
        tenant_id=_TENANT,
    )

    assert outbox.transitions[0].organization_id is None
