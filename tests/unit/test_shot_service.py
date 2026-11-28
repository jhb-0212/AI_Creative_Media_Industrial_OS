"""ShotService 的父场次屏障（SC-5 的并发侧）。

为什么这一侧必须单独测：归档侧修好之后，不变量仍然可以从另一端被破坏——归档
事务锁住父行、判定「无在产镜头」、正在写状态；此刻另一个事务把镜头推入在产并
提交。两侧都不报错，库里却出现「已归档场次带着在产镜头」，而那些镜头永远失去
可推进的父级。

三条判据：
1. 父场次已归档 → 进入在产被拒，且**状态未变、无迁移事实**；
2. 父场次正常 → 放行，且确实取了共享锁（不是只查了一次）；
3. 非在产边不取锁——屏障是边的属性，不是所有转移的固定成本。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from packages.errors.error_codes import DomainError
from packages.shared.status_transition import TransitionEvent, TransitionFact
from services.project_planning.domain.enums import SceneStatus, ShotStatus
from services.project_planning.domain.rework_task import ReworkTask
from services.project_planning.domain.scene import Scene
from services.project_planning.domain.shot import Shot
from services.project_planning.domain.state_machine import SHOT_LIFECYCLE
from services.project_planning.repository.shot_planning_repositories import (
    ReworkTaskRepository,
    SceneRepository,
    ShotRepository,
)
from services.project_planning.service.shot_service import ShotService

_TENANT = uuid4()


class _Shots(ShotRepository):
    """内存镜头仓储：只记录落库，不模拟读回。"""

    def __init__(self, shot: Shot) -> None:
        self.rows: dict[str, Shot] = {shot.entity_id: shot}
        self.saved: list[tuple[str, int]] = []

    async def existing_ids_in_project(self, project_id: str, shot_ids: list[str]) -> set[str]:
        # 本文件不建约束包：镜头归属查询只服务 CONSTRAINT.PACK.CREATE。
        # 让它直接失败，是为了把「别处误用这个端口」变成红灯，而不是返回一个
        # 看起来合理的空集（空集会让「镜头都不属于本项目」变成静默通过）。
        raise AssertionError("本用例不涉及约束包，不应查询镜头归属")

    async def add(self, shot: Shot) -> None:
        self.rows[shot.entity_id] = shot

    async def save(self, shot: Shot, expected_version: int) -> None:
        self.saved.append((shot.entity_id, expected_version))
        self.rows[shot.entity_id] = shot

    async def get_by_id(self, entity_id: str) -> Shot | None:
        return self.rows.get(entity_id)

    async def get_by_project_code(self, project_id: str, shot_code: str) -> Shot | None:
        for shot in self.rows.values():
            if shot.project_id == project_id and shot.shot_code == shot_code:
                return shot
        return None

    async def has_with_status(self, scene_id: str, status: ShotStatus) -> bool:
        raise AssertionError("ShotService 不该查子对象：它只关心父场次")


class _Scenes(SceneRepository):
    """内存场次仓储：记录锁的取用（这是本文件的主要观察点）。"""

    def __init__(self, scenes: list[Scene]) -> None:
        self.rows: dict[str, Scene] = {s.entity_id: s for s in scenes}
        self.locked: list[str] = []

    async def add(self, scene: Scene) -> None:
        self.rows[scene.entity_id] = scene

    async def save(self, scene: Scene, expected_version: int) -> None:
        self.rows[scene.entity_id] = scene

    async def get_by_id(self, entity_id: str) -> Scene | None:
        return self.rows.get(entity_id)

    async def get_by_project_code(self, project_id: str, scene_code: str) -> Scene | None:
        for scene in self.rows.values():
            if scene.project_id == project_id and scene.scene_code == scene_code:
                return scene
        return None

    async def lock_for_archive(self, entity_id: str) -> Scene | None:
        raise AssertionError("镜头侧的写入不该取排他锁：那会把归档路径串行化")

    async def lock_for_child_write(self, entity_id: str) -> Scene | None:
        self.locked.append(entity_id)
        return self.rows.get(entity_id)


class _ReworkTasks(ReworkTaskRepository):
    """返工任务仓储：本文件不涉及返工边，只提供空实现。"""

    def __init__(self) -> None:
        self.rows: dict[str, ReworkTask] = {}

    async def add(self, task: ReworkTask) -> None:
        self.rows[task.entity_id] = task

    async def save(self, task: ReworkTask, expected_version: int) -> None:
        self.rows[task.entity_id] = task

    async def get_by_id(self, entity_id: str) -> ReworkTask | None:
        return self.rows.get(entity_id)


@dataclass
class _Outbox:
    events: list[tuple[str, str]] = field(default_factory=list)
    transitions: list[TransitionFact] = field(default_factory=list)

    def record(
        self,
        *,
        aggregate_type: str,
        aggregate_id: str,
        tenant_id: UUID,
        event_type: str,
        payload: dict[str, object],
    ) -> UUID:
        self.events.append((aggregate_type, event_type))
        return uuid4()

    def record_transition(self, *, fact: TransitionFact, event: TransitionEvent) -> UUID:
        self.transitions.append(fact)
        return uuid4()


def _scene(status: SceneStatus = SceneStatus.ACTIVE) -> Scene:
    return Scene(
        entity_id="scene-1",
        project_id="proj-1",
        episode_id="ep-1",
        scene_code="S01",
        status=status,
        created_by="user-1",
        tenant_id=_TENANT,
    )


def _shot(status: ShotStatus = ShotStatus.NOT_STARTED) -> Shot:
    return Shot(
        entity_id="shot-1",
        project_id="proj-1",
        scene_id="scene-1",
        shot_code="S01-01",
        shot_revision_id="rev-1",
        status=status,
        created_by="user-1",
        tenant_id=_TENANT,
    )


def _service(*, shot: Shot, scenes: _Scenes) -> tuple[ShotService, _Shots, _Outbox]:
    shots = _Shots(shot)
    outbox = _Outbox()
    service = ShotService(shots, _ReworkTasks(), scenes, outbox)  # type: ignore[arg-type]
    return service, shots, outbox


@pytest.mark.asyncio
async def test_entering_production_under_archived_scene_is_denied() -> None:
    shot = _shot()
    scenes = _Scenes([_scene(SceneStatus.ARCHIVED)])
    service, shots, outbox = _service(shot=shot, scenes=scenes)

    with pytest.raises(DomainError) as exc:
        await service.change_status(
            entity_id=shot.entity_id,
            target_status=ShotStatus.IN_PRODUCTION,
            changed_by="user-1",
            tenant_id=_TENANT,
        )

    assert exc.value.error_code.code == "STATE_TRANSITION_DENIED"
    # 拒绝的说明来自边表声明，不是调用点手写——两处各写一次就允许某处写错。
    assert "父场次已归档" in exc.value.detail
    assert scenes.locked == [shot.scene_id], "拒绝前必须先确认父行（共享锁）"
    assert shots.saved == []
    assert outbox.transitions == []


@pytest.mark.asyncio
async def test_entering_production_under_active_scene_takes_shared_lock() -> None:
    shot = _shot()
    scenes = _Scenes([_scene()])
    service, shots, outbox = _service(shot=shot, scenes=scenes)

    await service.change_status(
        entity_id=shot.entity_id,
        target_status=ShotStatus.IN_PRODUCTION,
        changed_by="user-1",
        tenant_id=_TENANT,
    )

    assert scenes.locked == [shot.scene_id]
    assert len(shots.saved) == 1
    assert len(outbox.transitions) == 1
    assert outbox.transitions[0].to_status == ShotStatus.IN_PRODUCTION.value


@pytest.mark.asyncio
async def test_non_production_edge_takes_no_parent_lock() -> None:
    """屏障是边的属性：进入 BLOCKED 与父场次归档互不干扰，不该付锁的代价。"""
    shot = _shot()
    scenes = _Scenes([_scene()])
    service, shots, _ = _service(shot=shot, scenes=scenes)

    await service.change_status(
        entity_id=shot.entity_id,
        target_status=ShotStatus.BLOCKED,
        changed_by="user-1",
        tenant_id=_TENANT,
        blocked_reason="器材未到位",
    )

    assert scenes.locked == []
    assert len(shots.saved) == 1


@pytest.mark.asyncio
async def test_missing_parent_scene_is_not_found_not_silent_success() -> None:
    """父行读不到（已删 / 不在作用域）必须显式失败，不能当作「未归档」放行。"""
    shot = _shot()
    scenes = _Scenes([])
    service, shots, outbox = _service(shot=shot, scenes=scenes)

    with pytest.raises(DomainError) as exc:
        await service.change_status(
            entity_id=shot.entity_id,
            target_status=ShotStatus.IN_PRODUCTION,
            changed_by="user-1",
            tenant_id=_TENANT,
        )

    assert exc.value.error_code.code == "NOT_FOUND"
    assert shots.saved == []
    assert outbox.transitions == []


def test_reworked_shot_rolls_back_through_the_same_barrier() -> None:
    """返工回退（PRODUCTION_COMPLETE → IN_PRODUCTION）也进入在产，必须同样受控。"""
    precondition = SHOT_LIFECYCLE.parent_precondition(
        ShotStatus.PRODUCTION_COMPLETE, ShotStatus.IN_PRODUCTION
    )

    assert precondition is not None, "返工回退边漏了父场次屏障：可从这条边绕过归档判定"
    assert precondition.forbidden_status == SceneStatus.ARCHIVED.value


@pytest.mark.asyncio
async def test_cancellation_ref_is_rejected_before_side_effects() -> None:
    """A14 裁定：带 cancellation_ref 的推进在服务层入口即拒绝。

    必须证明三件事同时成立：拒绝、实体未被污染、审计事实（authority_ref）
    未落库——只断言「抛异常」会让「悄悄写了一半」的实现通过。
    """
    shot = _shot()
    scenes = _Scenes([_scene()])
    service, shots, outbox = _service(shot=shot, scenes=scenes)

    with pytest.raises(DomainError) as exc:
        await service.change_status(
            entity_id=shot.entity_id,
            target_status=ShotStatus.CANCELLED,
            changed_by="user-1",
            tenant_id=_TENANT,
            cancellation_ref="approval://self-declared/1",
            cancelled_at=datetime(2026, 11, 20, 12, 0, tzinfo=UTC),
        )

    assert exc.value.error_code.code == "STATE_TRANSITION_DENIED"
    assert "无法解析" in exc.value.detail
    assert shot.status is ShotStatus.NOT_STARTED, "拒绝后实体状态不得改变"
    assert shot.cancellation_ref is None, "自报引用不得写进正式对象"
    assert shots.saved == [], "拒绝后不得落库"
    assert outbox.transitions == [], "拒绝后不得写审计事实（含 authority_ref）"
    assert scenes.locked == [], "取消守卫在入口拒绝，不该走到父锁查询"


@pytest.mark.asyncio
async def test_cross_project_rework_basis_is_denied_before_write() -> None:
    """P1-03：返工依据必须同项目——库层复合外键之外，服务层先给可读拒绝。"""
    from services.project_planning.domain.enums import ReworkTaskStatus

    shot = _shot()  # project_id = proj-1
    scenes = _Scenes([_scene()])
    service, shots, outbox = _service(shot=shot, scenes=scenes)
    # 塞一个属于另一项目的返工任务到仓储
    cross = ReworkTask(
        entity_id="rework-other-project",
        project_id="proj-2",
        issue_id="issue-1",
        target_ref="asset-1",
        defect_category="画面",
        severity="HIGH",
        root_cause="灯光不足",
        owner="artist",
        return_node="LIGHTING",
        created_by="qc",
        tenant_id=_TENANT,
        status=ReworkTaskStatus.OPEN,
    )
    service._rework_task_repository.rows[cross.entity_id] = cross  # type: ignore[attr-defined]

    with pytest.raises(DomainError) as exc:
        await service.change_status(
            entity_id=shot.entity_id,
            target_status=ShotStatus.IN_PRODUCTION,
            changed_by="user-1",
            tenant_id=_TENANT,
            rework_basis_ref=cross.entity_id,
        )

    assert exc.value.error_code.code == "STATE_TRANSITION_DENIED"
    assert "项目" in exc.value.detail and "proj-2" in exc.value.detail
    assert shots.saved == [] and outbox.transitions == [], "拒绝后不得落库或写审计"
