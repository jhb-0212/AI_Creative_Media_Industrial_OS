"""ScriptService 输入归一化（strip）与空值校验的单元测试（M2/L3）。

使用内存 fake 仓储，验证：
- project_id / episode_ref / title 首尾空格在查询与入库前统一 strip。
- 空值（纯空格）字段在服务层被拒绝（VALIDATION_ERROR）。
- 归属验证：project 不存在、episode 不属于该项目时拒绝。
- 身份从 Principal 解析（created_by = principal.subject_id）。
"""

from __future__ import annotations

import uuid
from uuid import uuid4

import pytest

from packages.errors.error_codes import DomainError
from packages.identity.principal import Principal
from services.project_planning.domain.episode import Episode
from services.project_planning.domain.project import Project
from services.project_planning.domain.script import ScriptRevision
from services.project_planning.repository.episode_repo import EpisodeRepository
from services.project_planning.repository.outbox_writer import OutboxWriter
from services.project_planning.repository.project_repo import ProjectRepository
from services.project_planning.repository.script_repo import ScriptRepository
from services.project_planning.service.script_service import ScriptService

_TENANT = uuid.UUID("00000000-0000-0000-0000-000000000000")


def _principal(subject_id: str = "user_001") -> Principal:
    return Principal(subject_id=subject_id, tenant_id=_TENANT, roles=frozenset({"编剧"}))


class _RecordingOutboxWriter(OutboxWriter):
    """内存 outbox 写入 fake。"""

    def __init__(self) -> None:
        self.events: list[dict] = []

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


class _RecordingRepository(ScriptRepository):
    """记录每次调用参数的 fake 仓储，用于断言 strip 语义。"""

    def __init__(self) -> None:
        self._store: dict[str, ScriptRevision] = {}
        self.latest_calls: list[tuple[str, str]] = []

    async def add(self, script: ScriptRevision) -> None:
        self._store[script.entity_id] = script

    async def save(self, script: ScriptRevision, expected_version: int) -> None:
        self._store[script.entity_id] = script

    async def get_by_id(self, entity_id: str) -> ScriptRevision | None:
        return self._store.get(entity_id)

    async def get_latest_by_episode(
        self, project_id: str, episode_ref: str
    ) -> ScriptRevision | None:
        self.latest_calls.append((project_id, episode_ref))
        for s in self._store.values():
            if (
                s.project_id == project_id
                and s.episode_ref == episode_ref
                and s.status.value != "SUPERSEDED"
            ):
                return s
        return None


class _FakeProjectRepository(ProjectRepository):
    """内存 project 仓储，用于归属验证。"""

    def __init__(self, existing: list[str] | None = None) -> None:
        self._ids = set(existing or [])

    async def add(self, project: Project) -> None:
        self._ids.add(project.entity_id)

    async def save(self, project: Project, expected_version: int) -> None:
        self._ids.add(project.entity_id)

    async def get_by_id(self, entity_id: str) -> Project | None:
        if entity_id not in self._ids:
            return None
        return Project(
            entity_id=entity_id,
            project_name="p",
            project_tier="B",  # type: ignore[arg-type]
            created_by="u",
            tenant_id=_TENANT,
        )


class _FakeEpisodeRepository(EpisodeRepository):
    """内存 episode 仓储，用于归属验证。"""

    def __init__(self, owned: list[tuple[str, str]] | None = None) -> None:
        self._owned = set(owned or [])

    async def add(self, episode: Episode) -> None:
        self._owned.add((episode.project_id, episode.episode_ref))

    async def save(self, episode: Episode, expected_version: int) -> None:
        self._owned.add((episode.project_id, episode.episode_ref))

    async def get_by_id(self, entity_id: str) -> Episode | None:
        return None

    async def get_by_project_ref(
        self, project_id: str, episode_ref: str
    ) -> Episode | None:
        if (project_id, episode_ref) not in self._owned:
            return None
        return Episode(
            entity_id="ep1",
            project_id=project_id,
            episode_ref=episode_ref,
            title="e",
            created_by="u",
            tenant_id=_TENANT,
        )


def _make_service(
    script_repo: ScriptRepository,
    *,
    projects: list[str] | None = None,
    episodes: list[tuple[str, str]] | None = None,
) -> ScriptService:
    """构造 ScriptService，注入归属验证所需的 project/episode fake。

    默认提供一个「项目 proj_001 + 集数 (proj_001, EP001)」的合法归属，
    使正常路径测试聚焦 strip 语义本身。
    """
    default_projects = ["proj_001"]
    default_episodes = [("proj_001", "EP001")]
    return ScriptService(
        script_repo,
        _FakeProjectRepository(projects if projects is not None else default_projects),
        _FakeEpisodeRepository(episodes if episodes is not None else default_episodes),
        _RecordingOutboxWriter(),
    )


async def test_strip_normalizes_query_and_store() -> None:
    """带首尾空格的 project_id/episode_ref 应在查询与入库前被 strip。"""
    repo = _RecordingRepository()
    service = _make_service(repo)
    script = await service.import_script(
        project_id="  proj_001  ",
        episode_ref="  EP001  ",
        title="  测试剧本  ",
        principal=_principal("user_001"),
    )
    # 查询 latest 时用的是 strip 后的值
    assert repo.latest_calls == [("proj_001", "EP001")]
    # 入库的领域对象字段已 strip
    assert script.project_id == "proj_001"
    assert script.episode_ref == "EP001"
    assert script.title == "测试剧本"
    assert script.created_by == "user_001"  # 来自 Principal，非请求体


async def test_rejects_whitespace_only_episode_ref() -> None:
    repo = _RecordingRepository()
    service = _make_service(repo)
    with pytest.raises(DomainError) as exc_info:
        await service.import_script(
            project_id="proj_001",
            episode_ref="   ",
            title="标题",
            principal=_principal(),
        )
    assert exc_info.value.error_code.code == "VALIDATION_ERROR"


async def test_rejects_whitespace_only_created_by() -> None:
    """空 subject_id 在 Principal 构造层即被拒绝（ValueError）。

    身份治理：空主体无法通过 Principal 构造，故服务层不会再收到空 created_by。
    这是「身份来源可信」的构造级防呆，而非运行时空串校验。
    """
    with pytest.raises(ValueError, match="subject_id 不能为空"):
        Principal(subject_id="  ", tenant_id=_TENANT, roles=frozenset())


async def test_rejects_missing_project() -> None:
    """归属验证：project 不存在时拒绝（NOT_FOUND）。"""
    repo = _RecordingRepository()
    service = _make_service(repo, projects=[])
    with pytest.raises(DomainError) as exc_info:
        await service.import_script(
            project_id="proj_missing",
            episode_ref="EP001",
            title="标题",
            principal=_principal(),
        )
    assert exc_info.value.error_code.code == "NOT_FOUND"


async def test_rejects_episode_not_owned_by_project() -> None:
    """归属验证：episode 不属于该项目时拒绝（VALIDATION_ERROR）。"""
    repo = _RecordingRepository()
    service = _make_service(repo, episodes=[("proj_001", "EP999")])
    with pytest.raises(DomainError) as exc_info:
        await service.import_script(
            project_id="proj_001",
            episode_ref="EP001",  # 不属于该项目的集数
            title="标题",
            principal=_principal(),
        )
    assert exc_info.value.error_code.code == "VALIDATION_ERROR"
