"""Shot 运行时用例的行为验证（V205-R14-A14）。

为什么这些测试必须存在
----------------------
Shot 此前只有契约与数据库前置，没有运行时入口。仅测「能创建」不足以证明闭环：
正式镜头只能由 ``SHOT.REVISION.CONFIRM`` 诞生，状态只能由 ``SHOT.STATUS.CHANGE``
推进，版本指针前移必须留下事实/事件，且所有拒绝点都不能留下半条写入。

因此本文件把结果钉成可失败断言：
1. 首版确认原子创建 Shot + ShotRevision，并写审计事件；
2. 后续确认新增修订、旧版转 SUPERSEDED、指针前移，且事实行 owner_action 正确；
3. 同版重放不新增写入；错版本、错动作、缺守卫字段、缺返工依据全部拒绝；
4. 终态不可复活，父链冻结在确认前生效。
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.shared.outbox_writer import OutboxWriter
from services.project_planning.domain.enums import (
    EpisodeStatus,
    ProjectStatus,
    SceneStatus,
    ShotRevisionStatus,
    ShotStatus,
)
from services.project_planning.domain.episode import Episode
from services.project_planning.domain.project import Project
from services.project_planning.domain.scene import Scene
from services.project_planning.domain.shot import Shot
from services.project_planning.domain.state_machine import (
    SHOT_REVISION_CONFIRM_ACTION,
    SHOT_STATUS_CHANGE_ACTION,
)
from services.project_planning.repository.episode_repo import EpisodeRepository
from services.project_planning.repository.in_memory_repo import InMemorySceneRepository
from services.project_planning.repository.in_memory_shot_repo import (
    InMemoryReworkTaskVerifier,
    InMemoryShotRepository,
    InMemoryShotRevisionRepository,
)
from services.project_planning.repository.project_repo import ProjectRepository
from services.project_planning.service.shot_service import (
    ConfirmRevisionCommand,
    ShotService,
)

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


class _EpisodeRepo(EpisodeRepository):
    def __init__(self, episode: Episode | None, calls: list[str]) -> None:
        self._episode = episode
        self._calls = calls
        self.child_write_reads = 0

    async def add(self, episode: Episode) -> None:  # pragma: no cover
        raise AssertionError("本用例不创建集数")

    async def save(self, episode: Episode, expected_version: int) -> None:  # pragma: no cover
        raise AssertionError("本用例不保存集数")

    async def get_by_id(self, entity_id: str) -> Episode | None:
        return self._episode

    async def get_by_project_ref(
        self, project_id: str, episode_ref: str
    ) -> Episode | None:
        return self._episode

    async def get_by_project_ref_for_child_write(
        self, project_id: str, episode_ref: str
    ) -> Episode | None:
        self.child_write_reads += 1
        self._calls.append(f"Episode:{episode_ref}")
        return self._episode


class _RecordingShotRepo(InMemoryShotRepository):
    def __init__(self) -> None:
        super().__init__()
        self.added: list[Shot] = []
        self.saved: list[Shot] = []

    async def add(self, shot: Shot) -> None:
        await super().add(shot)
        self.added.append(shot)

    async def save(self, shot: Shot, expected_version: int) -> None:
        await super().save(shot, expected_version)
        self.saved.append(shot)


class _RecordingRevisionRepo(InMemoryShotRevisionRepository):
    def __init__(self) -> None:
        super().__init__()
        self.added: list[object] = []
        self.saved: list[object] = []

    async def add(self, revision: object) -> None:  # type: ignore[override]
        await super().add(revision)  # type: ignore[arg-type]
        self.added.append(revision)

    async def save(self, revision: object, expected_version: int) -> None:  # type: ignore[override]
        await super().save(revision, expected_version)  # type: ignore[arg-type]
        self.saved.append(revision)


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


def _project(status: ProjectStatus) -> Project:
    return Project(
        entity_id="p-1",
        project_name="probe",
        created_by="u",
        tenant_id=_TENANT,
        status=status,
        organization_id=_ORG,
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


def _scene() -> Scene:
    return Scene(
        entity_id="scene-1",
        project_id="p-1",
        episode_id="ep-1",
        scene_code="S001",
        created_by="u",
        tenant_id=_TENANT,
        status=SceneStatus.DRAFT,
        organization_id=_ORG,
    )


def _command(**overrides: object) -> ConfirmRevisionCommand:
    values: dict[str, object] = {
        "project_id": "p-1",
        "scene_id": "scene-1",
        "shot_code": "SH001",
        "description": "推轨近景",
        "narrative_intent": "建立压迫感",
        "location_ref": "loc/warehouse",
        "basis_script_revision_id": "sr-1",
        "script_anchors": ("anchor-1",),
        "character_refs": ("char-1",),
        "prop_refs": (),
        "constraint_pack_applicability": "NOT_APPLICABLE",
        "creative_autonomy": "L1",
        "risk": {"level": "LOW"},
        "quality_profile": "UNIFIED_HIGH_QUALITY",
        "intent_ref": {"intent_id": "intent-1", "intent_version": 1},
        "source_draft_ref": {"draft_id": "draft-1", "draft_version": 1},
        "source_candidate_refs": (),
        "change_origin": "BREAKDOWN_CONFIRM",
        "provenance": {},
    }
    values.update(overrides)
    return ConfirmRevisionCommand(**values)  # type: ignore[arg-type]


async def _service(
    project_status: ProjectStatus = ProjectStatus.ACTIVE,
    episode_status: EpisodeStatus = EpisodeStatus.ACTIVE,
) -> tuple[
    ShotService,
    _RecordingShotRepo,
    _RecordingRevisionRepo,
    InMemoryReworkTaskVerifier,
    _Outbox,
    list[str],
]:
    calls: list[str] = []
    scenes = InMemorySceneRepository()
    # 场景仓储写入是测试夹具，不经过服务，清零下方断言不受影响。
    await scenes.add(_scene())
    shots = _RecordingShotRepo()
    revisions = _RecordingRevisionRepo()
    rework = InMemoryReworkTaskVerifier()
    outbox = _Outbox()
    service = ShotService(
        shots=shots,
        revisions=revisions,
        scenes=scenes,
        episodes=_EpisodeRepo(_episode(episode_status), calls),
        projects=_ProjectRepo(_project(project_status), calls),
        rework_tasks=rework,
        outbox_writer=outbox,
    )
    return service, shots, revisions, rework, outbox, calls


async def _confirm_first(
    svc: ShotService, **overrides: object
) -> tuple[Shot, object]:
    return await svc.confirm_revision(
        _command(**overrides), confirmed_by="director", tenant_id=_TENANT
    )


# --------------------------------------------------------------------------- #
# 1. 正向可达：确认是唯一正式入口
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_first_confirm_atomically_creates_shot_and_revision() -> None:
    svc, shots, revisions, _, outbox, calls = await _service()

    shot, revision = await _confirm_first(svc)

    assert shot.shot_id == shot.entity_id
    assert shot.status is ShotStatus.NOT_STARTED
    assert shot.project_id == "p-1"
    assert shot.scene_id == "scene-1"
    assert shot.organization_id == _ORG
    assert shot.shot_revision_id == revision.entity_id
    assert revision.status is ShotRevisionStatus.ACTIVE
    assert revision.revision_no == 1
    assert len(shots.added) == 1
    assert len(revisions.added) == 1
    assert shots.saved == []  # 首版不经过指针前移
    assert calls == ["Project:p-1", "Episode:EP001"]
    assert [event["event_type"] for event in outbox.events] == [
        "SHOT_REVISION_CONFIRMED"
    ]
    assert outbox.events[0]["payload"]["shot_revision_id"] == shot.shot_revision_id


# --------------------------------------------------------------------------- #
# 2. 后续确认：新修订 + 旧版 SUPERSEDED + 指针前移
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_next_confirm_supersedes_head_and_advances_pointer() -> None:
    svc, shots, revisions, _, outbox, _ = await _service()
    shot, head = await _confirm_first(svc)
    shots.added.clear()
    revisions.added.clear()
    outbox.events.clear()

    next_shot, next_revision = await svc.confirm_revision(
        _command(
            shot_id=shot.shot_id,
            expected_shot_revision_id=head.entity_id,
            description="改为手持特写",
            change_summary="换镜头语言",
        ),
        confirmed_by="director",
        tenant_id=_TENANT,
    )

    assert next_shot.shot_id == shot.shot_id
    assert next_shot.shot_revision_id == next_revision.entity_id
    assert next_shot.entity_version == 2
    assert next_revision.revision_no == 2
    assert next_revision.supersedes_shot_revision_id == head.entity_id
    assert len(revisions.added) == 1
    assert shots.saved == [next_shot]
    assert not shots.added  # 后续确认不得再建 Shot

    stored_head = await revisions.get_by_id(head.entity_id)
    assert stored_head is not None
    assert stored_head.status is ShotRevisionStatus.SUPERSEDED
    assert len(revisions.saved) == 1
    assert len(outbox.transitions) == 1
    fact, superseded_event = outbox.transitions[0]
    assert fact.owner_action == SHOT_REVISION_CONFIRM_ACTION
    assert fact.resource_type == "ShotRevision"
    assert superseded_event.event_type == "SHOT_REVISION_SUPERSEDED"
    # 指针前移是事件型变更，不写 status_transitions；因此普通事件只有确认这一条。
    assert [event["event_type"] for event in outbox.events] == [
        "SHOT_REVISION_CONFIRMED"
    ]


@pytest.mark.asyncio
async def test_same_revision_replay_does_not_write_again() -> None:
    svc, shots, revisions, _, outbox, _ = await _service()
    shot, head = await _confirm_first(svc)
    shots.added.clear()
    revisions.added.clear()
    outbox.events.clear()

    replayed_shot, replayed_revision = await svc.confirm_revision(
        _command(
            shot_id=shot.shot_id,
            expected_shot_revision_id=head.entity_id,
        ),
        confirmed_by="director",
        tenant_id=_TENANT,
    )

    assert replayed_shot.entity_id == shot.entity_id
    assert replayed_revision.entity_id == head.entity_id
    assert shots.added == []
    assert revisions.added == []
    assert shots.saved == []
    assert outbox.events == []
    assert outbox.transitions == []


@pytest.mark.asyncio
async def test_wrong_expected_revision_is_conflict() -> None:
    svc, shots, revisions, _, outbox, _ = await _service()
    shot, head = await _confirm_first(svc)

    with pytest.raises(DomainError) as exc:
        await svc.confirm_revision(
            _command(
                shot_id=shot.shot_id,
                expected_shot_revision_id="stale-revision",
                description="另一个内容",
                change_summary="冲突修订",
            ),
            confirmed_by="director",
            tenant_id=_TENANT,
        )

    assert exc.value.error_code.code == ErrorRegistry.IDEMPOTENCY_CONFLICT.code
    assert len(revisions.added) == 1  # 只有夹具里的首版
    assert shots.saved == []
    assert len(outbox.events) == 1  # 冲突路径不得新增事件
    assert outbox.events[0]["event_type"] == "SHOT_REVISION_CONFIRMED"
    assert head.entity_id != "stale-revision"


# --------------------------------------------------------------------------- #
# 3. 状态推进：守卫字段、返工依据与终态
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_blocked_without_reason_is_denied_without_write() -> None:
    svc, shots, _, _, outbox, _ = await _service()
    shot, _ = await _confirm_first(svc)
    shots.saved.clear()
    outbox.transitions.clear()

    with pytest.raises(DomainError) as exc:
        await svc.change_status(
            entity_id=shot.shot_id,
            target_status=ShotStatus.BLOCKED,
            changed_by="director",
            tenant_id=_TENANT,
        )

    assert exc.value.error_code.code == ErrorRegistry.STATE_TRANSITION_DENIED.code
    assert "blocked_reason" in exc.value.detail
    assert shots.saved == []
    assert outbox.transitions == []


@pytest.mark.asyncio
async def test_blocked_with_reason_writes_fact_with_shot_action() -> None:
    svc, shots, _, _, outbox, _ = await _service()
    shot, _ = await _confirm_first(svc)

    changed = await svc.change_status(
        entity_id=shot.shot_id,
        target_status=ShotStatus.BLOCKED,
        changed_by="director",
        tenant_id=_TENANT,
        blocked_reason="缺资产",
    )

    assert changed.status is ShotStatus.BLOCKED
    assert changed.blocked_reason == "缺资产"
    fact, event = outbox.transitions[-1]
    assert fact.owner_action == SHOT_STATUS_CHANGE_ACTION
    assert fact.from_status == "NOT_STARTED"
    assert fact.to_status == "BLOCKED"
    assert event.event_type == "SHOT_STATUS_CHANGED"
    assert len(shots.saved) == 1


@pytest.mark.asyncio
async def test_rework_requires_real_task_in_same_scope() -> None:
    svc, _shots, _, rework, outbox, _ = await _service()
    shot, _ = await _confirm_first(svc)
    for target in (ShotStatus.IN_PRODUCTION, ShotStatus.PRODUCTION_COMPLETE):
        await svc.change_status(
            entity_id=shot.shot_id,
            target_status=target,
            changed_by="director",
            tenant_id=_TENANT,
        )
    outbox.transitions.clear()

    with pytest.raises(DomainError) as exc:
        await svc.change_status(
            entity_id=shot.shot_id,
            target_status=ShotStatus.IN_PRODUCTION,
            changed_by="director",
            tenant_id=_TENANT,
            rework_basis_ref="task-1",
        )
    assert exc.value.error_code.code == ErrorRegistry.VALIDATION_ERROR.code
    assert outbox.transitions == []

    rework.add(
        "task-1", project_id="p-1", tenant_id=_TENANT, organization_id=_ORG
    )
    changed = await svc.change_status(
        entity_id=shot.shot_id,
        target_status=ShotStatus.IN_PRODUCTION,
        changed_by="director",
        tenant_id=_TENANT,
        rework_basis_ref="task-1",
    )
    assert changed.status is ShotStatus.IN_PRODUCTION
    assert changed.rework_basis_ref == "task-1"
    assert len(outbox.transitions) == 1


@pytest.mark.asyncio
async def test_cancel_is_fail_closed_without_authorization_object() -> None:
    svc, shots, _, _, outbox, _ = await _service()
    shot, _ = await _confirm_first(svc)
    shots.saved.clear()
    outbox.transitions.clear()

    with pytest.raises(DomainError) as exc:
        await svc.change_status(
            entity_id=shot.shot_id,
            target_status=ShotStatus.CANCELLED,
            changed_by="director",
            tenant_id=_TENANT,
            cancellation_ref="anything",
        )

    assert exc.value.error_code.code == ErrorRegistry.VALIDATION_ERROR.code
    assert "fail-closed" in exc.value.detail
    assert shots.saved == []
    assert outbox.transitions == []


def test_wrong_action_cannot_own_shot_transition_or_pointer_move() -> None:
    shot = Shot(
        entity_id="shot-1",
        created_by="u",
        tenant_id=_TENANT,
        shot_code="SH001",
        scene_id="scene-1",
        project_id="p-1",
        shot_revision_id="rev-1",
        organization_id=_ORG,
    )

    with pytest.raises(DomainError) as denied:
        shot.apply_status_change(
            ShotStatus.IN_PRODUCTION, "audit:1", "SCENE.STATUS.CHANGE"
        )
    assert denied.value.error_code.code == ErrorRegistry.STATE_TRANSITION_DENIED.code
    assert shot.status is ShotStatus.NOT_STARTED

    with pytest.raises(DomainError) as denied_pointer:
        shot.point_to_revision("rev-2", "audit:1", SHOT_STATUS_CHANGE_ACTION)
    assert (
        denied_pointer.value.error_code.code
        == ErrorRegistry.STATE_TRANSITION_DENIED.code
    )
    assert shot.shot_revision_id == "rev-1"


@pytest.mark.asyncio
async def test_cancelled_shot_cannot_be_revived() -> None:
    svc, shots, _, _, outbox, _ = await _service()
    cancelled = Shot(
        entity_id="shot-cancelled",
        created_by="u",
        tenant_id=_TENANT,
        shot_code="SH-CANCELLED",
        scene_id="scene-1",
        project_id="p-1",
        shot_revision_id="rev-cancelled",
        status=ShotStatus.CANCELLED,
        cancellation_ref="authorization-1",
        organization_id=_ORG,
    )
    await shots.add(cancelled)
    shots.saved.clear()

    with pytest.raises(DomainError) as exc:
        await svc.change_status(
            entity_id=cancelled.entity_id,
            target_status=ShotStatus.NOT_STARTED,
            changed_by="director",
            tenant_id=_TENANT,
        )

    assert exc.value.error_code.code == ErrorRegistry.STATE_TRANSITION_DENIED.code
    assert shots.saved == []
    assert outbox.transitions == []


# --------------------------------------------------------------------------- #
# 4. 父链冻结：确认前逐级判定
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "project_status", [ProjectStatus.ARCHIVED, ProjectStatus.COMPLETED]
)
async def test_frozen_project_blocks_confirmation(project_status: ProjectStatus) -> None:
    svc, shots, _, _, outbox, calls = await _service(project_status=project_status)

    with pytest.raises(DomainError) as exc:
        await _confirm_first(svc)

    assert exc.value.error_code.code == ErrorRegistry.PARENT_LIFECYCLE_FROZEN.code
    assert "Project" in exc.value.detail
    assert calls == ["Project:p-1", "Episode:EP001"]
    assert shots.added == []
    assert outbox.events == []


@pytest.mark.asyncio
async def test_archived_episode_blocks_confirmation() -> None:
    svc, shots, _, _, outbox, _ = await _service(episode_status=EpisodeStatus.ARCHIVED)

    with pytest.raises(DomainError) as exc:
        await _confirm_first(svc)

    assert exc.value.error_code.code == ErrorRegistry.PARENT_LIFECYCLE_FROZEN.code
    assert "Episode" in exc.value.detail
    assert shots.added == []
    assert outbox.events == []
