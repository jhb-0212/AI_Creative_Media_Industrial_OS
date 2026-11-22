"""Scene 运行时用例的行为验证（V205-R14-A13）。

为什么这些测试不是「没抛异常就算过」
-----------------------------------
Scene 的失败形态在历史里出现过两种：一是只判直接父不判祖父，二是幂等与冻结
顺序写反——两者在正常路径上完全看不出来，只有特定的重放/归档时序才会暴露。
因此本文件把四类结果都钉成可失败断言：

1. **正向可达**：父链未冻结时必须真的写入，并写对归属键与事件；
2. **拒绝点正确**：Project / Episode 两级分别拒绝，且错误码与 detail 可区分；
3. **顺序可证明**：用共享调用记录断言锁顺序为 Project → Episode；
4. **重放仍成立**：父链归档后，同 entity_id 重放返回原结果，不新增写、不新增事件。

状态推进部分同时断言 ``TransitionFact.owner_action``，防止「事件写了、准入
没走 SCENE.STATUS.CHANGE」这种只留痕不把关的退化。
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.shared.outbox_writer import OutboxWriter
from packages.shared.port_errors import DuplicateEntityError
from packages.shared.versioning import ConcurrentModificationError
from services.project_planning.domain.enums import (
    EpisodeStatus,
    ProjectStatus,
    SceneStatus,
    ShotStatus,
)
from services.project_planning.domain.episode import Episode
from services.project_planning.domain.project import Project
from services.project_planning.domain.scene import Scene
from services.project_planning.domain.shot import Shot
from services.project_planning.domain.state_machine import (
    EPISODE_LIFECYCLE,
    PROJECT_LIFECYCLE,
    SCENE_STATUS_CHANGE_ACTION,
)
from services.project_planning.repository.episode_repo import EpisodeRepository
from services.project_planning.repository.in_memory_repo import InMemorySceneRepository
from services.project_planning.repository.project_repo import ProjectRepository
from services.project_planning.repository.scene_repo import SceneRepository
from services.project_planning.repository.shot_planning_repositories import (
    ShotRepository,
)
from services.project_planning.service.scene_service import SceneService

_TENANT = UUID("00000000-0000-0000-0000-0000000000aa")
_ORG = UUID("00000000-0000-0000-0000-0000000000bb")


class _ProjectRepo(ProjectRepository):
    def __init__(self, project: Project | None, calls: list[str]) -> None:
        self._project = project
        self._calls = calls
        self.child_write_reads = 0

    async def add(self, project: Project) -> None:  # pragma: no cover
        raise AssertionError("本用例不创建项目")

    async def save(self, project: Project, expected_version: int) -> None:  # pragma: no cover
        raise AssertionError("本用例不保存项目")

    async def get_by_id(self, entity_id: str) -> Project | None:
        return self._project

    async def get_by_id_for_child_write(self, entity_id: str) -> Project | None:
        self.child_write_reads += 1
        self._calls.append(f"Project:{entity_id}")
        return self._project


class _ShotRepo(ShotRepository):
    """内存镜头仓储：本文件不推进镜头状态，只承载 SC-5 的存在性查询。"""

    def __init__(self, shots: list[Shot] | None = None) -> None:
        self.rows: dict[str, Shot] = {s.entity_id: s for s in (shots or [])}

    async def existing_ids_in_project(
        self, project_id: str, shot_ids: list[str]
    ) -> set[str]:
        # 本文件不建约束包：镜头归属查询只服务 CONSTRAINT.PACK.CREATE。
        # 让它直接失败，是为了把「别处误用这个端口」变成红灯，而不是返回一个
        # 看起来合理的空集（空集会让「镜头都不属于本项目」变成静默通过）。
        raise AssertionError("本用例不涉及约束包，不应查询镜头归属")

    async def add(self, shot: Shot) -> None:
        self.rows[shot.entity_id] = shot

    async def save(self, shot: Shot, expected_version: int) -> None:
        self.rows[shot.entity_id] = shot

    async def get_by_id(self, entity_id: str) -> Shot | None:
        return self.rows.get(entity_id)

    async def get_by_project_code(self, project_id: str, shot_code: str) -> Shot | None:
        for shot in self.rows.values():
            if shot.project_id == project_id and shot.shot_code == shot_code:
                return shot
        return None

    async def has_with_status(self, scene_id: str, status: ShotStatus) -> bool:
        return any(
            shot.scene_id == scene_id and shot.status is status
            for shot in self.rows.values()
        )


class _EpisodeRepo(EpisodeRepository):
    def __init__(self, episodes: list[Episode], calls: list[str]) -> None:
        self._episodes = episodes
        self._calls = calls
        self.child_write_reads = 0

    async def add(self, episode: Episode) -> None:  # pragma: no cover
        raise AssertionError("本用例不创建集数")

    async def save(self, episode: Episode, expected_version: int) -> None:  # pragma: no cover
        raise AssertionError("本用例不保存集数")

    async def get_by_id(self, entity_id: str) -> Episode | None:
        return next((e for e in self._episodes if e.entity_id == entity_id), None)

    async def get_by_project_ref(
        self, project_id: str, episode_ref: str
    ) -> Episode | None:
        return next(
            (
                e
                for e in self._episodes
                if e.project_id == project_id and e.episode_ref == episode_ref
            ),
            None,
        )

    async def get_by_project_ref_for_child_write(
        self, project_id: str, episode_ref: str
    ) -> Episode | None:
        self.child_write_reads += 1
        self._calls.append(f"Episode:{episode_ref}")
        return await self.get_by_project_ref(project_id, episode_ref)


class _RecordingSceneRepo(InMemorySceneRepository):
    """在真实端口语义之上记录写入次数，证明拒绝发生在 INSERT 之前。"""

    def __init__(self) -> None:
        super().__init__()
        self.added: list[Scene] = []
        self.saved: list[Scene] = []

    async def add(self, scene: Scene) -> None:
        await super().add(scene)
        self.added.append(scene)

    async def save(self, scene: Scene, expected_version: int) -> None:
        await super().save(scene, expected_version)
        self.saved.append(scene)


class _ConflictOnSaveSceneRepo(_RecordingSceneRepo):
    """把乐观锁冲突做成可控输入，验证服务层翻译成结构化错误。"""

    async def save(self, scene: Scene, expected_version: int) -> None:
        raise ConcurrentModificationError(scene.entity_id, expected_version)


class _Outbox(OutboxWriter):
    def __init__(self) -> None:
        self.events: list[dict] = []
        self.transitions: list[tuple[object, object]] = []

    def record(  # type: ignore[override]
        self,
        *,
        aggregate_type,
        aggregate_id,
        tenant_id,
        event_type,
        payload,
    ) -> UUID:
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
        self.transitions.append((fact, event))
        return uuid4()


def _project(status: ProjectStatus, *, organization_id: UUID | None = _ORG) -> Project:
    return Project(
        entity_id="p-1",
        project_name="probe",
        created_by="u",
        tenant_id=_TENANT,
        status=status,
        organization_id=organization_id,
    )


def _episode(status: EpisodeStatus) -> Episode:
    return Episode(
        entity_id="ep-1",
        project_id="p-1",
        episode_ref="EP001",
        title="第一集",
        created_by="u",
        tenant_id=_TENANT,
        status=status,
    )


def _service(
    project: Project | None,
    episodes: list[Episode] | None = None,
    *,
    scenes: SceneRepository | None = None,
) -> tuple[SceneService, _RecordingSceneRepo, _ProjectRepo, _EpisodeRepo, _Outbox, list[str]]:
    calls: list[str] = []
    scene_repo = scenes if isinstance(scenes, _RecordingSceneRepo) else _RecordingSceneRepo()
    project_repo = _ProjectRepo(project, calls)
    episode_repo = _EpisodeRepo(episodes if episodes is not None else [], calls)
    outbox = _Outbox()
    service = SceneService(
        repository=scene_repo,
        episode_repository=episode_repo,
        project_repository=project_repo,
        shot_repository=_ShotRepo(),
        outbox_writer=outbox,
    )
    return service, scene_repo, project_repo, episode_repo, outbox, calls


async def _create(
    svc: SceneService, *, entity_id: str | None = "scene-fixed", scene_code: str = "S001"
) -> Scene:
    return await svc.create_scene(
        project_id="p-1",
        episode_ref="EP001",
        scene_code=scene_code,
        created_by="u",
        tenant_id=_TENANT,
        entity_id=entity_id,
    )


# --------------------------------------------------------------------------- #
# 1. 正向可达：不证明能写，后面所有「拒绝」都可能是恒拒绝
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_active_parent_creates_scene_with_derived_identity_and_event() -> None:
    svc, scenes, projects, episodes, outbox, calls = _service(
        _project(ProjectStatus.ACTIVE), [_episode(EpisodeStatus.ACTIVE)]
    )

    scene = await _create(svc)

    assert scene.entity_id == "scene-fixed"
    assert scene.scene_id == "scene-fixed"  # identity 投影，不是第二份数据
    assert scene.project_id == "p-1"
    assert scene.episode_id == "ep-1"
    assert scene.organization_id == _ORG  # 从父项目拷贝，非调用方自填
    assert scene.status is SceneStatus.DRAFT
    assert scenes.added == [scene]
    assert calls == ["Project:p-1", "Episode:EP001"]  # 固定自顶向下，避免死锁
    assert projects.child_write_reads == 1
    assert episodes.child_write_reads == 1
    assert [event["event_type"] for event in outbox.events] == ["SCENE_CREATED"]
    assert outbox.events[0]["payload"] == {
        "scene_id": "scene-fixed",
        "project_id": "p-1",
        "episode_id": "ep-1",
        "scene_code": "S001",
        "created_by": "u",
        "tenant_id": str(_TENANT),
        "status": "DRAFT",
    }


def test_scene_serialization_does_not_leak_internal_ownership_keys() -> None:
    scene = Scene(
        entity_id="scene-fixed",
        created_by="u",
        tenant_id=_TENANT,
        project_id="p-1",
        episode_id="ep-1",
        scene_code="S001",
        organization_id=_ORG,
    )
    data = scene.to_dict()
    assert data["scene_id"] == data["entity_id"] == "scene-fixed"
    assert "tenant_id" not in data
    assert "organization_id" not in data


# --------------------------------------------------------------------------- #
# 2. 父链双冻结：只判直接父会被「上层归档、下层仍 ACTIVE」绕过
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
@pytest.mark.parametrize("project_status", [ProjectStatus.ARCHIVED, ProjectStatus.COMPLETED])
async def test_frozen_project_blocks_creation_even_when_episode_active(
    project_status: ProjectStatus,
) -> None:
    svc, scenes, _, episodes, outbox, calls = _service(
        _project(project_status), [_episode(EpisodeStatus.ACTIVE)]
    )

    with pytest.raises(DomainError) as exc:
        await _create(svc)

    assert exc.value.error_code.code == ErrorRegistry.PARENT_LIFECYCLE_FROZEN.code
    assert "Project" in exc.value.detail
    # 两级都先锁住，才能在幂等回读里确认请求所指的集；冻结判定在锁之后。
    # 这里锁 Episode 不产生写，也不会让「上层冻结」被绕过。
    assert episodes.child_write_reads == 1
    assert calls == ["Project:p-1", "Episode:EP001"]
    assert scenes.added == []
    assert outbox.events == []


@pytest.mark.asyncio
async def test_archived_episode_blocks_creation() -> None:
    svc, scenes, _, _, outbox, calls = _service(
        _project(ProjectStatus.ACTIVE), [_episode(EpisodeStatus.ARCHIVED)]
    )

    with pytest.raises(DomainError) as exc:
        await _create(svc)

    assert exc.value.error_code.code == ErrorRegistry.PARENT_LIFECYCLE_FROZEN.code
    assert "Episode" in exc.value.detail
    assert calls == ["Project:p-1", "Episode:EP001"]
    assert scenes.added == []
    assert outbox.events == []


@pytest.mark.asyncio
async def test_missing_project_is_not_found_not_frozen() -> None:
    """父不存在与父冻结必须可区分，前者是「给错 id」而非「封存」。"""
    svc, scenes, _, episodes, _, _ = _service(None, [_episode(EpisodeStatus.ACTIVE)])

    with pytest.raises(DomainError) as exc:
        await _create(svc)

    assert exc.value.error_code.code == ErrorRegistry.NOT_FOUND.code
    assert episodes.child_write_reads == 0
    assert scenes.added == []


@pytest.mark.asyncio
async def test_episode_from_another_project_is_rejected() -> None:
    episode = _episode(EpisodeStatus.ACTIVE)
    episode.project_id = "p-other"
    svc, scenes, _, _, _, _ = _service(_project(ProjectStatus.ACTIVE), [episode])

    with pytest.raises(DomainError) as exc:
        await _create(svc)

    assert exc.value.error_code.code == ErrorRegistry.VALIDATION_ERROR.code
    assert "不属于项目" in exc.value.detail
    assert scenes.added == []


@pytest.mark.asyncio
async def test_project_without_organization_key_is_rejected_diagnosably() -> None:
    """个人租户项目不能建场次：给可诊断 4xx，不把它伪装成并发冲突。"""
    svc, scenes, _, _, outbox, _ = _service(
        _project(ProjectStatus.ACTIVE, organization_id=None),
        [_episode(EpisodeStatus.ACTIVE)],
    )

    with pytest.raises(DomainError) as exc:
        await _create(svc)

    assert exc.value.error_code.code == ErrorRegistry.VALIDATION_ERROR.code
    assert "组织归属" in exc.value.detail
    assert scenes.added == []
    assert outbox.events == []


# --------------------------------------------------------------------------- #
# 3. 幂等：重放不是新建，归档后也必须能确认首次结果
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_replay_after_project_archive_returns_original_without_new_write() -> None:
    svc, scenes, _, _, outbox, _ = _service(
        _project(ProjectStatus.ARCHIVED), [_episode(EpisodeStatus.ACTIVE)]
    )
    original = Scene(
        entity_id="scene-fixed",
        created_by="u",
        tenant_id=_TENANT,
        project_id="p-1",
        episode_id="ep-1",
        scene_code="S001",
        organization_id=_ORG,
    )
    await scenes.add(original)
    scenes.added.clear()  # 夹具写入不是服务新增，清零后断言才有意义

    replayed = await _create(svc)

    assert replayed.entity_id == original.entity_id
    assert replayed.scene_code == original.scene_code
    assert scenes.added == []
    assert outbox.events == []


@pytest.mark.asyncio
async def test_freeze_guards_are_actual_call_sites(monkeypatch: pytest.MonkeyPatch) -> None:
    """把两级守卫换成哨兵：调用点在就报哨兵，被删掉则本用例失败。

    这不是「用异常证明异常」：上面已有行为用例证明正常语义。这里证明的是
    **行为用例依赖的守卫真的在调用链上**，防止有人把判定改成死代码后，
    行为用例因其他偶然原因仍通过。
    """
    svc, _, _, _, _, _ = _service(
        _project(ProjectStatus.ACTIVE), [_episode(EpisodeStatus.ACTIVE)]
    )

    def project_boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("PROJECT_FREEZE_GUARD_CALLED")

    # Lifecycle 是 frozen dataclass，不能替换实例属性；替换类方法即可覆盖调用点。
    monkeypatch.setattr(
        type(PROJECT_LIFECYCLE), "assert_child_creation_allowed", project_boom
    )
    with pytest.raises(AssertionError, match="PROJECT_FREEZE_GUARD_CALLED"):
        await _create(svc)

    svc, _, _, _, _, _ = _service(
        _project(ProjectStatus.ACTIVE), [_episode(EpisodeStatus.ACTIVE)]
    )

    def episode_boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("EPISODE_FREEZE_GUARD_CALLED")

    monkeypatch.setattr(
        type(EPISODE_LIFECYCLE), "assert_child_creation_allowed", episode_boom
    )
    with pytest.raises(AssertionError, match="EPISODE_FREEZE_GUARD_CALLED"):
        await _create(svc)


@pytest.mark.asyncio
async def test_idempotency_runs_before_freeze_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """变异式顺序断言：把冻结守卫换成哨兵，重放请求不得触发它。

    若有人把冻结判定挪回幂等之前，这条会立刻失败——这正是本次修掉的
    「归档后重试变 409」缺陷的复发闸门。
    """
    svc, scenes, _, _, outbox, _ = _service(
        _project(ProjectStatus.ARCHIVED), [_episode(EpisodeStatus.ACTIVE)]
    )
    await scenes.add(
        Scene(
            entity_id="scene-fixed",
            created_by="u",
            tenant_id=_TENANT,
            project_id="p-1",
            episode_id="ep-1",
            scene_code="S001",
            organization_id=_ORG,
        )
    )

    def boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("FREEZE_GUARD_RAN_BEFORE_IDEMPOTENCY")

    monkeypatch.setattr(
        type(PROJECT_LIFECYCLE), "assert_child_creation_allowed", boom
    )
    replayed = await _create(svc)

    assert replayed.entity_id == "scene-fixed"
    assert outbox.events == []


@pytest.mark.asyncio
async def test_reusing_entity_id_for_different_request_is_conflict() -> None:
    svc, scenes, _, _, _, _ = _service(
        _project(ProjectStatus.ACTIVE), [_episode(EpisodeStatus.ACTIVE)]
    )
    await scenes.add(
        Scene(
            entity_id="scene-fixed",
            created_by="u",
            tenant_id=_TENANT,
            project_id="p-1",
            episode_id="ep-1",
            scene_code="S001",
            organization_id=_ORG,
        )
    )

    with pytest.raises(DomainError) as exc:
        await _create(svc, scene_code="S002")

    assert exc.value.error_code.code == ErrorRegistry.IDEMPOTENCY_CONFLICT.code


@pytest.mark.asyncio
async def test_duplicate_project_code_returns_existing_scene() -> None:
    """无显式 id 的重复请求靠 (project_id, scene_code) 唯一键找回既有行。"""
    svc, _, _, _, outbox, _ = _service(
        _project(ProjectStatus.ACTIVE), [_episode(EpisodeStatus.ACTIVE)]
    )
    first = await _create(svc, entity_id=None)

    second = await _create(svc, entity_id=None)

    assert second.entity_id == first.entity_id
    assert len(outbox.events) == 1


def test_in_memory_repo_enforces_project_scene_code_uniqueness() -> None:
    """内存仓储若只守主键，单测会通过、真实库唯一冲突——最难查的环境差异。"""
    import asyncio

    repo = InMemorySceneRepository()
    scene = Scene(
        entity_id="scene-1",
        created_by="u",
        tenant_id=_TENANT,
        project_id="p-1",
        episode_id="ep-1",
        scene_code="S001",
    )
    duplicate = Scene(
        entity_id="scene-2",
        created_by="u",
        tenant_id=_TENANT,
        project_id="p-1",
        episode_id="ep-2",
        scene_code="S001",
    )

    async def run() -> None:
        await repo.add(scene)
        with pytest.raises(DuplicateEntityError) as exc:
            await repo.add(duplicate)
        assert exc.value.constraint == "uq_scenes_project_code"

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# 4. 状态推进：动作所有权、事实行与终态不可复活
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_change_status_uses_scene_action_and_writes_transition_fact() -> None:
    svc, scenes, _, _, outbox, _ = _service(
        _project(ProjectStatus.ACTIVE), [_episode(EpisodeStatus.ACTIVE)]
    )
    scene = await _create(svc)

    changed = await svc.change_status(
        entity_id=scene.entity_id,
        target_status=SceneStatus.ACTIVE,
        changed_by="director",
        tenant_id=_TENANT,
    )

    assert changed.status is SceneStatus.ACTIVE
    assert changed.entity_version == 2
    assert [saved.entity_id for saved in scenes.saved] == [scene.entity_id]
    assert scenes.saved[0].status is SceneStatus.ACTIVE
    assert scenes.saved[0].entity_version == 2
    assert len(outbox.transitions) == 1
    fact, event = outbox.transitions[0]
    assert fact.owner_action == SCENE_STATUS_CHANGE_ACTION
    assert fact.resource_type == "Scene"
    assert fact.from_status == "DRAFT"
    assert fact.to_status == "ACTIVE"
    assert fact.actor_subject == "director"
    assert fact.organization_id == _ORG
    assert event.event_type == "SCENE_STATUS_CHANGED"


@pytest.mark.asyncio
async def test_archived_scene_cannot_be_revived() -> None:
    _, scenes, _, _, outbox, _ = _service(
        _project(ProjectStatus.ACTIVE), [_episode(EpisodeStatus.ACTIVE)]
    )
    archived = Scene(
        entity_id="scene-archived",
        created_by="u",
        tenant_id=_TENANT,
        project_id="p-1",
        episode_id="ep-1",
        scene_code="S-ARCHIVED",
        status=SceneStatus.ARCHIVED,
        organization_id=_ORG,
    )
    await scenes.add(archived)
    svc = SceneService(
        repository=scenes,
        episode_repository=_EpisodeRepo([_episode(EpisodeStatus.ACTIVE)], []),
        project_repository=_ProjectRepo(_project(ProjectStatus.ACTIVE), []),
        shot_repository=_ShotRepo(),
        outbox_writer=outbox,
    )

    with pytest.raises(DomainError) as exc:
        await svc.change_status(
            entity_id=archived.entity_id,
            target_status=SceneStatus.ACTIVE,
            changed_by="director",
            tenant_id=_TENANT,
        )

    assert exc.value.error_code.code == ErrorRegistry.STATE_TRANSITION_DENIED.code
    assert scenes.saved == []
    assert outbox.transitions == []


@pytest.mark.asyncio
async def test_optimistic_lock_conflict_becomes_structured_error() -> None:
    conflict_repo = _ConflictOnSaveSceneRepo()
    svc, _, _, _, outbox, _ = _service(
        _project(ProjectStatus.ACTIVE),
        [_episode(EpisodeStatus.ACTIVE)],
        scenes=conflict_repo,
    )
    scene = await _create(svc)

    with pytest.raises(DomainError) as exc:
        await svc.change_status(
            entity_id=scene.entity_id,
            target_status=SceneStatus.ACTIVE,
            changed_by="director",
            tenant_id=_TENANT,
        )

    assert exc.value.error_code.code == ErrorRegistry.CONCURRENT_MODIFICATION.code
    assert outbox.transitions == []  # 事实/事件不得在写失败后留下


def test_wrong_action_cannot_own_scene_transition() -> None:
    scene = Scene(
        entity_id="scene-1",
        created_by="u",
        tenant_id=_TENANT,
        project_id="p-1",
        episode_id="ep-1",
        scene_code="S001",
    )

    with pytest.raises(DomainError) as exc:
        scene.transition_to(SceneStatus.ACTIVE, "audit:1", "EPISODE.STATUS.CHANGE")

    assert exc.value.error_code.code == ErrorRegistry.STATE_TRANSITION_DENIED.code
    assert scene.status is SceneStatus.DRAFT


# --------------------------------------------------------------------------- #
# A13 正式链（episode_id 寻址）也必须判父链冻结
# --------------------------------------------------------------------------- #
async def _create_by_episode_id(
    svc: SceneService, *, entity_id: str | None = "scene-fixed", scene_code: str = "S001"
) -> Scene:
    return await svc.create_scene(
        project_id="p-1",
        episode_id="ep-1",
        scene_code=scene_code,
        created_by="u",
        tenant_id=_TENANT,
        entity_id=entity_id,
    )


@pytest.mark.asyncio
async def test_episode_id_addressing_rejects_archived_episode() -> None:
    """这条寻址线此前完全不判冻结（注释声称「由数据库守卫承担」，而库里没有）。

    缺口的形态是「文档说有、实际没有」，所以本用例只钉行为：归档集数下不得新增
    场次，且拒绝发生在 INSERT 之前。
    """
    svc, scenes, _, _, outbox, _ = _service(
        _project(ProjectStatus.ACTIVE), [_episode(EpisodeStatus.ARCHIVED)]
    )

    with pytest.raises(DomainError) as exc:
        await _create_by_episode_id(svc)

    assert exc.value.error_code is ErrorRegistry.PARENT_LIFECYCLE_FROZEN
    assert scenes.added == []
    assert outbox.events == []


@pytest.mark.asyncio
async def test_episode_id_addressing_rejects_archived_project() -> None:
    """归档项目 + 仍 ACTIVE 的集数：只判直接父会漏掉上一层（对称路径绕过）。"""
    svc, scenes, _, _, _, _ = _service(
        _project(ProjectStatus.ARCHIVED), [_episode(EpisodeStatus.ACTIVE)]
    )

    with pytest.raises(DomainError) as exc:
        await _create_by_episode_id(svc)

    assert exc.value.error_code is ErrorRegistry.PARENT_LIFECYCLE_FROZEN
    assert scenes.added == []


@pytest.mark.asyncio
async def test_episode_id_addressing_locks_the_whole_parent_chain() -> None:
    """两条寻址线取锁顺序必须一致（Project → Episode），否则互相死锁。"""
    svc, _, projects, episodes, _, calls = _service(
        _project(ProjectStatus.ACTIVE), [_episode(EpisodeStatus.ACTIVE)]
    )

    await _create_by_episode_id(svc)

    assert calls == ["Project:p-1", "Episode:EP001"]
    assert projects.child_write_reads == 1
    assert episodes.child_write_reads == 1


@pytest.mark.asyncio
async def test_episode_id_addressing_keeps_organization_null_for_personal_tenant() -> None:
    """放行时组织键仍为 None：A13 链不要求组织归属，个人租户项目合法。"""
    svc, scenes, _, _, _, _ = _service(
        _project(ProjectStatus.ACTIVE, organization_id=None),
        [_episode(EpisodeStatus.ACTIVE)],
    )

    scene = await _create_by_episode_id(svc)

    assert scene.organization_id is None
    assert scenes.added == [scene]
