"""故障传播契约骨架：依赖故障时必须 fail-closed，不得静默吞成成功。

范围声明（与 README 一致）
--------------------------
本文件验证的是**单机可复现的故障传播契约**：Outbox 端口故障时，服务层必须让
异常冒泡、不得产生"状态推进成功但审计事实缺失"的假成功。它**不是**目标环境的
韧性验收（压测/故障演练/恢复）——那些缺口登记在生产准入清单，未被本文件勾选。

判据（fail-closed，不把"没抛异常"当 PASS）
------------------------------------------
1. 正常路径作为对照组必须真的推进（防止把服务改成永远拒绝来"通过"负向测试）；
2. 注入 Outbox 故障时，异常必须冒泡（断言异常类型 + 消息，不是 `not raised`）；
3. 故障时不得留下迁移事实（审计事实与事件是同一序列，缺一即为非法状态）。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID, uuid4

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.shared.status_transition import TransitionEvent, TransitionFact  # noqa: E402
from services.project_planning.domain.enums import SceneStatus, ShotStatus  # noqa: E402
from services.project_planning.domain.scene import Scene  # noqa: E402
from services.project_planning.domain.shot import Shot  # noqa: E402
from services.project_planning.repository.shot_planning_repositories import (  # noqa: E402
    ReworkTaskRepository,
    SceneRepository,
    ShotRepository,
)
from services.project_planning.service.shot_service import ShotService  # noqa: E402

_TENANT = uuid4()


class _OutageError(RuntimeError):
    """模拟 Outbox 依赖不可用（如 DB 断连 / 写入失败）。"""


@dataclass
class _Outbox:
    """内存 Outbox；``fail`` 时模拟依赖不可用。"""

    transitions: list[TransitionFact] = field(default_factory=list)
    fail: bool = False

    def record(self, **_kwargs: object) -> UUID:
        if self.fail:
            raise _OutageError("outbox dependency unavailable")
        return uuid4()

    def record_transition(self, *, fact: TransitionFact, event: TransitionEvent) -> UUID:
        if self.fail:
            raise _OutageError("outbox dependency unavailable")
        self.transitions.append(fact)
        return uuid4()


class _Shots(ShotRepository):
    def __init__(self, shot: Shot) -> None:
        self.rows = {shot.entity_id: shot}
        self.saved: list[Shot] = []

    async def add(self, shot: Shot) -> None:
        self.rows[shot.entity_id] = shot

    async def save(self, shot: Shot, expected_version: int) -> None:
        self.saved.append(shot)
        self.rows[shot.entity_id] = shot

    async def get_by_id(self, entity_id: str) -> Shot | None:
        return self.rows.get(entity_id)

    async def get_by_project_code(self, project_id: str, shot_code: str) -> Shot | None:
        return None

    async def has_with_status(self, scene_id: str, status: ShotStatus) -> bool:
        return False


class _Scenes(SceneRepository):
    def __init__(self, scene: Scene) -> None:
        self.rows = {scene.entity_id: scene}

    async def add(self, scene: Scene) -> None:
        self.rows[scene.entity_id] = scene

    async def save(self, scene: Scene, expected_version: int) -> None:
        self.rows[scene.entity_id] = scene

    async def get_by_id(self, entity_id: str) -> Scene | None:
        return self.rows.get(entity_id)

    async def get_by_project_code(self, project_id: str, scene_code: str) -> Scene | None:
        return None

    async def lock_for_archive(self, entity_id: str) -> Scene | None:
        return self.rows.get(entity_id)

    async def lock_for_child_write(self, entity_id: str) -> Scene | None:
        return self.rows.get(entity_id)


class _ReworkTasks(ReworkTaskRepository):
    async def add(self, task: object) -> None:  # type: ignore[override]
        raise AssertionError("本骨架不涉及返工任务")

    async def save(self, task: object, expected_version: int) -> None:  # type: ignore[override]
        raise AssertionError("本骨架不涉及返工任务")

    async def get_by_id(self, entity_id: str) -> object | None:  # type: ignore[override]
        return None


def _shot() -> Shot:
    return Shot(
        entity_id="shot-1",
        project_id="proj-1",
        scene_id="scene-1",
        shot_code="S01-01",
        shot_revision_id="rev-1",
        status=ShotStatus.NOT_STARTED,
        created_by="user-1",
        tenant_id=_TENANT,
    )


def _scene() -> Scene:
    return Scene(
        entity_id="scene-1",
        project_id="proj-1",
        episode_id="ep-1",
        scene_code="S01",
        status=SceneStatus.ACTIVE,
        created_by="user-1",
        tenant_id=_TENANT,
    )


def _service(outbox: _Outbox) -> tuple[ShotService, _Shots]:
    shots = _Shots(_shot())
    service = ShotService(shots, _ReworkTasks(), _Scenes(_scene()), outbox)  # type: ignore[arg-type]
    return service, shots


@pytest.mark.asyncio
async def test_control_group_normal_path_actually_advances() -> None:
    """对照组：依赖正常时状态确实推进且写入事实——防止负向测试靠'永远拒绝'过关。"""
    outbox = _Outbox()
    service, _shots = _service(outbox)

    changed = await service.change_status(
        entity_id="shot-1",
        target_status=ShotStatus.IN_PRODUCTION,
        changed_by="user-1",
        tenant_id=_TENANT,
    )

    assert changed.status is ShotStatus.IN_PRODUCTION
    assert len(outbox.transitions) == 1


@pytest.mark.asyncio
async def test_outbox_outage_propagates_and_leaves_no_transition_fact() -> None:
    """Outbox 故障时异常必须冒泡，且不得留下迁移事实（fail-closed）。"""
    outbox = _Outbox(fail=True)
    service, _shots = _service(outbox)

    with pytest.raises(_OutageError, match="unavailable"):
        await service.change_status(
            entity_id="shot-1",
            target_status=ShotStatus.IN_PRODUCTION,
            changed_by="user-1",
            tenant_id=_TENANT,
        )

    # 审计事实与事件是同一序列：故障时不得留下"状态推进了却没事实"的假成功。
    assert outbox.transitions == [], "依赖故障时必须 fail-closed，不得留下半截审计事实"
