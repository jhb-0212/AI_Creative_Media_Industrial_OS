"""父对象冻结在 ``SCRIPT.IMPORT`` 上的行为契约。

要证三件互相独立的事，缺一条就会得出错误结论：

1. 父集数已归档 → 导入新剧本版本被拒（``PARENT_LIFECYCLE_FROZEN``）。
   只测这一条不够：一个「永远拒绝」的实现同样能过。
2. 父集数 ACTIVE → 导入成功。这是对照组，证明拒绝确实来自冻结判定，
   而不是仓储装配坏了、或权杖恰好在别处断了。
3. 父集数已归档 → ``SCRIPT.LOCK.REQUEST``（审批推进）**仍然成功**。
   这条是防「修一处、误伤另一处」：冻结检查若塞进共用的
   ``_assert_episode_ownership``，会把「已归档集数下完成既有审批」一起拒掉。
   审批是对既有事实的补记，不是新建内容，两者语义不同。
"""

from __future__ import annotations

import uuid
from uuid import uuid4

import pytest

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.identity.principal import Principal
from packages.shared.outbox_writer import OutboxWriter
from services.project_planning.domain.enums import EpisodeStatus, ProjectStatus
from services.project_planning.domain.episode import Episode
from services.project_planning.domain.project import Project
from services.project_planning.domain.script import ScriptRevision
from services.project_planning.repository.episode_repo import EpisodeRepository
from services.project_planning.repository.project_repo import ProjectRepository
from services.project_planning.repository.script_repo import ScriptRepository
from services.project_planning.service.script_service import ScriptService

_TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")


class _Outbox(OutboxWriter):
    def __init__(self) -> None:
        self.transitions: list = []

    def record(self, *, aggregate_type, aggregate_id, tenant_id, event_type, payload):  # type: ignore[override]
        return uuid4()

    def record_transition(self, *, fact, event):  # type: ignore[override]
        self.transitions.append(fact)
        return uuid4()


class _ScriptRepo(ScriptRepository):
    def __init__(self, script: ScriptRevision | None = None) -> None:
        self._scripts: dict[str, ScriptRevision] = {}
        if script is not None:
            self._scripts[script.entity_id] = script

    async def add(self, script: ScriptRevision) -> None:
        self._scripts[script.entity_id] = script

    async def save(self, script: ScriptRevision, expected_version: int) -> None:
        self._scripts[script.entity_id] = script

    async def get_by_id(self, entity_id: str) -> ScriptRevision | None:
        return self._scripts.get(entity_id)

    async def get_latest_by_episode(
        self, project_id: str, episode_ref: str
    ) -> ScriptRevision | None:
        return None


class _ProjectRepo(ProjectRepository):
    def __init__(self, status: ProjectStatus = ProjectStatus.ACTIVE) -> None:
        self._status = status

    async def add(self, project) -> None:  # type: ignore[override]
        pass

    async def save(self, project, expected_version: int) -> None:  # type: ignore[override]
        pass

    async def get_by_id(self, entity_id: str):  # type: ignore[override]
        return Project(
            entity_id=entity_id,
            project_name="p",
            project_tier="B",  # type: ignore[arg-type]
            created_by="u",
            tenant_id=_TENANT,
            status=self._status,
        )

    async def get_by_id_for_child_write(self, entity_id: str):  # type: ignore[override]
        return await self.get_by_id(entity_id)


class _EpisodeRepo(EpisodeRepository):
    """集数状态可控——被测变量就是它。"""

    def __init__(self, status: EpisodeStatus) -> None:
        self._episode = Episode(
            entity_id="ep1",
            project_id="proj1",
            episode_ref="EP001",
            title="e",
            created_by="u",
            tenant_id=_TENANT,
            status=status,
        )
        self.locked_reads = 0

    async def add(self, episode: Episode) -> None:
        self._episode = episode

    async def save(self, episode: Episode, expected_version: int) -> None:
        self._episode = episode

    async def get_by_id(self, entity_id: str) -> Episode | None:
        return self._episode

    async def get_by_project_ref(
        self, project_id: str, episode_ref: str
    ) -> Episode | None:
        if (
            self._episode.project_id == project_id
            and self._episode.episode_ref == episode_ref
        ):
            return self._episode
        return None

    async def get_by_project_ref_for_child_write(
        self, project_id: str, episode_ref: str
    ) -> Episode | None:
        # 记一次调用，供断言「创建路径真的走了带锁读」——
        # 否则将来有人把 service 改回无锁版，冻结判定仍在，测试却全绿。
        self.locked_reads += 1
        return await self.get_by_project_ref(project_id, episode_ref)


def _principal(subject_id: str = "writer_001") -> Principal:
    return Principal(
        subject_id=subject_id, tenant_id=_TENANT, roles=frozenset({"导演", "编剧"})
    )


def _service(
    status: EpisodeStatus,
    *,
    script: ScriptRevision | None = None,
    project_status: ProjectStatus = ProjectStatus.ACTIVE,
):
    repo = _ScriptRepo(script)
    episodes = _EpisodeRepo(status)
    service = ScriptService(repo, _ProjectRepo(project_status), episodes, _Outbox())
    return service, episodes


async def test_import_rejected_when_episode_archived() -> None:
    """已归档集数下不得再导入剧本版本。"""
    service, episodes = _service(EpisodeStatus.ARCHIVED)

    with pytest.raises(DomainError) as exc:
        await service.import_script(
            project_id="proj1",
            episode_ref="EP001",
            title="第一稿",
            principal=_principal(),
        )

    assert exc.value.error_code.code == ErrorRegistry.PARENT_LIFECYCLE_FROZEN.code
    assert "ARCHIVED" in (exc.value.detail or ""), exc.value.detail
    assert episodes.locked_reads == 1, "创建路径没有走带锁读，并发下检查会穿透"


async def test_import_allowed_when_episode_active() -> None:
    """对照组：ACTIVE 集数下导入必须成功——否则上面的拒绝对任何实现都成立。"""
    service, episodes = _service(EpisodeStatus.ACTIVE)

    script = await service.import_script(
        project_id="proj1",
        episode_ref="EP001",
        title="第一稿",
        principal=_principal(),
    )

    assert script.status.value == "DRAFT"
    assert episodes.locked_reads == 1


async def test_lock_request_still_allowed_when_episode_archived() -> None:
    """审批推进不受冻结影响：已归档集数下仍可完成既有版本的锁定申请。

    这是「不误伤」的证据。若冻结检查塞进 ``_assert_episode_ownership``，
    这条会红——而那条路径上并没有创建任何新内容。
    """
    draft = ScriptRevision(
        entity_id="scr1",
        project_id="proj1",
        episode_ref="EP001",
        title="第一稿",
        created_by="u",
        tenant_id=_TENANT,
    )
    service, episodes = _service(EpisodeStatus.ARCHIVED, script=draft)

    updated = await service.request_lock(entity_id="scr1", principal=_principal())

    assert updated.status.value == "LOCK_PENDING"
    assert episodes.locked_reads == 0, "审批路径不应走带锁读（它不需要与归档相遇）"


async def test_project_archived_does_not_block_import_known_gap() -> None:
    """**已知缺口**：父链冻结未传递——Project 已归档，其下 Episode 仍可导入剧本。

    这不是「设计如此」，是尚未实现的传递性：`import_script` 只看直接父
    （Episode），不检查祖父（Project）。当前 Project 归档后，只要其下 Episode
    还是 ACTIVE，导入照样成功。

    钉住它的理由与 ``test_project_completed_currently_not_frozen`` 相同：
    缺口一旦被修，这条测试必须同步改，而不是让「修好了」与「一直是这样」
    在代码里分不清。同时防止它被误当成预期行为写进别处的假设。
    """
    service, _ = _service(
        EpisodeStatus.ACTIVE, project_status=ProjectStatus.ARCHIVED
    )

    script = await service.import_script(
        project_id="proj1",
        episode_ref="EP001",
        title="第一稿",
        principal=_principal(),
    )

    assert script.status.value == "DRAFT"
    assert script.entity_id, "导入确实成功了——这正是缺口"
