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

4. 父链传递：Project 冻结时，即便 Episode 仍 ACTIVE，导入也必须被拒
   （2026-11-21 补齐；此前只判直接父，属「只拦一半」缺口）。
   同时验证 COMPLETED 与 ARCHIVED 同等对待——只写死 ARCHIVED 的实现会漏。
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
        self.locked_reads = 0

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
        # 与 _EpisodeRepo 同口径：记一次带锁读，供断言「父链每一级都取到了锁」。
        self.locked_reads += 1
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
    projects = _ProjectRepo(project_status)
    service = ScriptService(repo, projects, episodes, _Outbox())
    return service, episodes, projects


async def test_import_rejected_when_episode_archived() -> None:
    """已归档集数下不得再导入剧本版本。"""
    service, episodes, projects = _service(EpisodeStatus.ARCHIVED)

    with pytest.raises(DomainError) as exc:
        await service.import_script(
            project_id="proj1",
            episode_ref="EP001",
            title="第一稿",
            principal=_principal(),
        )

    assert exc.value.error_code.code == ErrorRegistry.PARENT_LIFECYCLE_FROZEN.code
    assert "Episode" in (exc.value.detail or ""), exc.value.detail
    assert "ARCHIVED" in (exc.value.detail or ""), exc.value.detail
    assert episodes.locked_reads == 1, "创建路径没有走带锁读，并发下检查会穿透"
    # 父链自顶向下：Project 也在取锁之列（否则「Project 归档」这一层拦不住）。
    assert projects.locked_reads == 1


async def test_import_allowed_when_episode_active() -> None:
    """对照组：ACTIVE 集数下导入必须成功——否则上面的拒绝对任何实现都成立。"""
    service, episodes, projects = _service(EpisodeStatus.ACTIVE)

    script = await service.import_script(
        project_id="proj1",
        episode_ref="EP001",
        title="第一稿",
        principal=_principal(),
    )

    assert script.status.value == "DRAFT"
    assert episodes.locked_reads == 1
    assert projects.locked_reads == 1


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
    service, episodes, projects = _service(EpisodeStatus.ARCHIVED, script=draft)

    updated = await service.request_lock(entity_id="scr1", principal=_principal())

    assert updated.status.value == "LOCK_PENDING"
    assert episodes.locked_reads == 0, "审批路径不应走带锁读（它不需要与归档相遇）"
    assert projects.locked_reads == 0


async def test_project_archived_blocks_import_even_when_episode_active() -> None:
    """父链冻结传递：Project 归档 ⇒ 其下导入剧本被拒（即使 Episode 仍 ACTIVE）。

    这条替换了此前钉住缺口的 ``..._known_gap`` 用例。缺口的存在形态很具体：
    冻结只判直接父，于是「上层已封存、下层还开着」时写入照样成功——与「只拦
    归档侧、不拦推进侧」同型，都是**只拦一半**。修法不是加一个特例，而是让
    创建路径沿父链逐级判定。

    必须区分错误来源：这里拒的理由是 **Project** 冻结，不是 Episode 冻结
    （Episode 在本用例里就是 ACTIVE）。只断言「抛了 PARENT_LIFECYCLE_FROZEN」
    不够——那样把 Episode 设成 ARCHIVED 的实现同样能过，而它并没有修父链。
    """
    service, episodes, projects = _service(
        EpisodeStatus.ACTIVE, project_status=ProjectStatus.ARCHIVED
    )

    with pytest.raises(DomainError) as exc:
        await service.import_script(
            project_id="proj1",
            episode_ref="EP001",
            title="第一稿",
            principal=_principal(),
        )

    assert exc.value.error_code.code == ErrorRegistry.PARENT_LIFECYCLE_FROZEN.code
    assert "Project" in (exc.value.detail or ""), exc.value.detail
    assert "ARCHIVED" in (exc.value.detail or ""), exc.value.detail
    # 父级判定在建子行之前短路，故 Episode 这一级不该被读：先锁 Project 是刻意的
    # 顺序（自顶向下），也让最常见的拒绝在最小代价处发生。
    assert projects.locked_reads == 1
    assert episodes.locked_reads == 0


async def test_project_completed_blocks_import_even_when_episode_active() -> None:
    """`COMPLETED` 与 `ARCHIVED` 同为冻结态，父链判定不区分二者。

    这条防的是「只在 ARCHIVED 上写死」的实现：COMPLETED 有出边（→ARCHIVED），
    很容易被当成「还活着」而漏掉，但状态图没有回到 ACTIVE 的路，内容已定稿。
    """
    service, _, projects = _service(
        EpisodeStatus.ACTIVE, project_status=ProjectStatus.COMPLETED
    )

    with pytest.raises(DomainError) as exc:
        await service.import_script(
            project_id="proj1",
            episode_ref="EP001",
            title="第一稿",
            principal=_principal(),
        )

    assert exc.value.error_code.code == ErrorRegistry.PARENT_LIFECYCLE_FROZEN.code
    assert "COMPLETED" in (exc.value.detail or ""), exc.value.detail
    assert projects.locked_reads == 1
