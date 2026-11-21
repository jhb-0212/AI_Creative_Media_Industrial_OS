"""`EPISODE.CREATE` 必须受父对象生命周期约束——行为验证，不是「没报错」。

为什么这个文件存在
------------------
`PROJECT_LIFECYCLE.frozen_states` 声明得再对，只要没有调用点就是死代码；
而调用点接错位置（放在幂等判定之前、或放在插入之后）同样会让缺口存活。
所以这里测的是**服务层的行为**：

1. 项目 ACTIVE → 能建集数（否则实现退化成「一律拒绝」，第 2 条就毫无意义）；
2. 项目 ARCHIVED → 拒绝，且错误码是 ``PARENT_LIFECYCLE_FROZEN``，不是笼统的校验失败；
3. 项目 ARCHIVED + 同一个 entity_id 重放 → **仍返回原结果**（幂等优先于冻结），
   否则归档之后，调用方永远无法确认自己那次创建到底生效没有；
4. 判定发生在插入**之前**——用仓储是否被写入反向证明。
"""

from __future__ import annotations

from uuid import UUID

import pytest

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.shared.outbox_writer import OutboxWriter
from services.project_planning.domain.enums import ProjectStatus
from services.project_planning.domain.episode import Episode
from services.project_planning.domain.project import Project
from services.project_planning.repository.episode_repo import EpisodeRepository
from services.project_planning.repository.project_repo import ProjectRepository
from services.project_planning.service.episode_service import EpisodeService

_TENANT = UUID("00000000-0000-0000-0000-0000000000aa")


class _ProjectRepo(ProjectRepository):
    """只读假件：按 id 返回给定项目，记录是否走了「带锁读」路径。"""

    def __init__(self, project: Project | None) -> None:
        self._project = project
        self.child_write_reads = 0

    async def add(self, project: Project) -> None:  # pragma: no cover
        raise AssertionError("本用例不创建项目")

    async def save(self, project: Project, expected_version: int) -> None:  # pragma: no cover
        raise AssertionError("本用例不保存项目")

    async def get_by_id(self, entity_id: str) -> Project | None:
        return self._project

    async def get_by_id_for_child_write(self, entity_id: str) -> Project | None:
        self.child_write_reads += 1
        return self._project


class _EpisodeRepo(EpisodeRepository):
    def __init__(self) -> None:
        self.added: list[Episode] = []
        self._store: dict[str, Episode] = {}

    async def add(self, episode: Episode) -> None:
        self.added.append(episode)
        self._store[episode.entity_id] = episode

    async def save(self, episode: Episode, expected_version: int) -> None:  # pragma: no cover
        raise AssertionError("本用例不推进状态")

    async def get_by_id(self, entity_id: str) -> Episode | None:
        return self._store.get(entity_id)

    async def get_by_project_ref(self, project_id: str, episode_ref: str) -> Episode | None:
        for ep in self._store.values():
            if ep.project_id == project_id and ep.episode_ref == episode_ref:
                return ep
        return None


class _Outbox(OutboxWriter):
    def __init__(self) -> None:
        self.events: list[dict] = []

    def record(self, *, aggregate_type, aggregate_id, tenant_id, event_type, payload):  # type: ignore[override]
        self.events.append({"event_type": event_type, "aggregate_id": aggregate_id})
        return aggregate_id

    def record_transition(self, *, fact, event):  # type: ignore[override]  # pragma: no cover
        raise AssertionError("本用例不推进状态")


def _project(status: ProjectStatus) -> Project:
    return Project(
        entity_id="p-1",
        project_name="probe",
        created_by="u",
        tenant_id=_TENANT,
        status=status,
    )


def _service(project: Project | None) -> tuple[EpisodeService, _EpisodeRepo, _ProjectRepo]:
    episodes = _EpisodeRepo()
    projects = _ProjectRepo(project)
    return (
        EpisodeService(
            repository=episodes, project_repository=projects, outbox_writer=_Outbox()
        ),
        episodes,
        projects,
    )


async def _create(svc: EpisodeService, *, entity_id: str | None = None) -> Episode:
    return await svc.create_episode(
        project_id="p-1",
        episode_ref="EP001",
        title="第一集",
        created_by="u",
        tenant_id=_TENANT,
        entity_id=entity_id,
    )


@pytest.mark.asyncio
async def test_active_project_allows_episode_creation() -> None:
    """反向证伪：若这里也拒，说明实现是「恒拒绝」，下面的拒绝用例毫无意义。"""
    svc, episodes, projects = _service(_project(ProjectStatus.ACTIVE))
    episode = await _create(svc)
    assert episode.episode_ref == "EP001"
    assert len(episodes.added) == 1
    # 必须走带锁读，否则并发窗口仍在（FR-1 的根因不是「忘了判」，是「判的时候没锁」）
    assert projects.child_write_reads == 1


@pytest.mark.asyncio
async def test_archived_project_rejects_episode_creation() -> None:
    svc, episodes, _ = _service(_project(ProjectStatus.ARCHIVED))
    with pytest.raises(DomainError) as exc:
        await _create(svc)
    assert exc.value.error_code.code == ErrorRegistry.PARENT_LIFECYCLE_FROZEN.code
    assert "Episode" in exc.value.detail
    # 判定必须在插入之前：仓储一行都不该被写入
    assert episodes.added == []


@pytest.mark.asyncio
async def test_archived_project_still_idempotent_on_replay() -> None:
    """归档后重放「当时成功过」的创建请求 → 返回原结果，不是 409。

    这条不变量容易被「把冻结判定往前挪」破坏，而破坏后表现为：调用方超时重试
    拿到 409，无法判断首次是否成功，只能人工核对——正是幂等设计要消除的东西。
    """
    svc, episodes, _ = _service(_project(ProjectStatus.ARCHIVED))
    first = Episode(
        entity_id="ep-fixed",
        project_id="p-1",
        episode_ref="EP001",
        title="第一集",
        created_by="u",
        tenant_id=_TENANT,
    )
    await episodes.add(first)

    replayed = await _create(svc, entity_id="ep-fixed")
    assert replayed.entity_id == "ep-fixed"
    assert len(episodes.added) == 1  # 未新增第二行


@pytest.mark.asyncio
async def test_missing_project_still_not_found_not_frozen() -> None:
    """父不存在应报 NOT_FOUND，而不是被冻结判定掩盖成 409。

    两种失败必须可区分：前者「你给错了 id」，后者「项目已封存」。
    """
    svc, _, _ = _service(None)
    with pytest.raises(DomainError) as exc:
        await _create(svc)
    assert exc.value.error_code.code == ErrorRegistry.NOT_FOUND.code
