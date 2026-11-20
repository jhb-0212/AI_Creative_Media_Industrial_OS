"""SceneService 行为测试：SC-3 编号唯一域与 SC-5 归档前置条件。

为什么这两条必须有服务层用例（而不是只测领域层）：它们都不是聚合根能判定的
不变量——SC-3 的唯一域由**库层约束 + 幂等回读键**共同决定，SC-5 需要一个跨聚合
的存在性查询。只测领域层会让这两条永远「看起来实现了」，而实际调用路径上
没有生效。

三条判据：
1. 归档被拒：场次下存在 IN_PRODUCTION 的镜头时，归档必须失败，且**状态未变、
   无迁移事实**——「先改状态再回滚」不是拒绝。
2. 归档放行：无在产镜头时正常推进，并写出一条迁移事实。
3. 跨集撞号：项目内唯一被违反时，报「编号被占用」而不是「幂等键复用」——
   两者都是 409，但修复方向相反（一个换编号，一个重试）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from packages.errors.error_codes import DomainError
from packages.shared.port_errors import DuplicateEntityError
from packages.shared.status_transition import TransitionEvent, TransitionFact
from services.project_planning.domain.enums import SceneStatus, ShotStatus
from services.project_planning.domain.episode import Episode
from services.project_planning.domain.scene import Scene
from services.project_planning.domain.shot import Shot
from services.project_planning.repository.shot_planning_repositories import (
    SceneRepository,
    ShotRepository,
)
from services.project_planning.service.scene_service import SceneService

_NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
_TENANT = uuid4()


# --------------------------------------------------------------------------- #
# 替身：只实现被调用的语义，不做多余的事
# --------------------------------------------------------------------------- #
class _SceneStore(SceneRepository):
    """内存场次仓储。

    ``enforce_project_code`` 打开时按项目内唯一拒绝重复编号——它模拟的是**库层
    唯一约束**，不是服务层校验。测试要验的正是「服务层如何解释这个约束」。
    """

    def __init__(self, *, enforce_project_code: bool = True) -> None:
        self.rows: dict[str, Scene] = {}
        self.enforce_project_code = enforce_project_code
        self.saved: list[tuple[str, int]] = []

    async def add(self, scene: Scene) -> None:
        if scene.entity_id in self.rows:
            raise DuplicateEntityError(f"duplicate entity_id: {scene.entity_id}")
        if self.enforce_project_code and await self.get_by_project_code(
            scene.project_id, scene.scene_code
        ):
            raise DuplicateEntityError(f"duplicate scene_code: {scene.scene_code}")
        self.rows[scene.entity_id] = scene

    async def save(self, scene: Scene, expected_version: int) -> None:
        self.saved.append((scene.entity_id, expected_version))
        self.rows[scene.entity_id] = scene

    async def get_by_id(self, entity_id: str) -> Scene | None:
        return self.rows.get(entity_id)

    async def get_by_project_code(self, project_id: str, scene_code: str) -> Scene | None:
        for scene in self.rows.values():
            if scene.project_id == project_id and scene.scene_code == scene_code:
                return scene
        return None


class _ShotStore(ShotRepository):
    """内存镜头仓储：只让 ``has_with_status`` 有真实语义。"""

    def __init__(self, shots: list[Shot] | None = None) -> None:
        self.rows: dict[str, Shot] = {s.entity_id: s for s in (shots or [])}
        self.queries: list[tuple[str, ShotStatus]] = []

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
        self.queries.append((scene_id, status))
        return any(
            shot.scene_id == scene_id and shot.status is status
            for shot in self.rows.values()
        )


class _EpisodeStore:
    def __init__(self, episodes: list[Episode]) -> None:
        self._rows = {e.entity_id: e for e in episodes}

    async def get_by_id(self, entity_id: str) -> Episode | None:
        return self._rows.get(entity_id)


@dataclass
class _Outbox:
    """记录 outbox 调用，用于断言「拒绝时一条事实都没写」。"""

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

    def record_transition(
        self, *, fact: TransitionFact, event: TransitionEvent
    ) -> UUID:
        self.transitions.append(fact)
        return uuid4()


def _episode(project_id: str = "proj-1", episode_id: str = "ep-1") -> Episode:
    return Episode(
        entity_id=episode_id,
        project_id=project_id,
        episode_ref="EP01",
        title="第一集",
        created_by="user-1",
        tenant_id=_TENANT,
    )


def _scene(
    *,
    entity_id: str = "scene-1",
    episode_id: str = "ep-1",
    scene_code: str = "S01",
    status: SceneStatus = SceneStatus.DRAFT,
) -> Scene:
    return Scene(
        entity_id=entity_id,
        project_id="proj-1",
        episode_id=episode_id,
        scene_code=scene_code,
        status=status,
        created_by="user-1",
        tenant_id=_TENANT,
    )


def _shot(scene_id: str = "scene-1", status: ShotStatus = ShotStatus.IN_PRODUCTION) -> Shot:
    return Shot(
        entity_id="shot-1",
        project_id="proj-1",
        scene_id=scene_id,
        shot_code="S01-01",
        shot_revision_id="rev-1",
        status=status,
        created_by="user-1",
        tenant_id=_TENANT,
    )


def _service(
    *,
    scenes: _SceneStore | None = None,
    shots: _ShotStore | None = None,
    episodes: list[Episode] | None = None,
    outbox: _Outbox | None = None,
) -> tuple[SceneService, _SceneStore, _ShotStore, _Outbox]:
    scene_store = scenes or _SceneStore()
    shot_store = shots or _ShotStore()
    writer = outbox or _Outbox()
    service = SceneService(
        scene_store,
        _EpisodeStore(episodes if episodes is not None else [_episode()]),  # type: ignore[arg-type]
        writer,  # type: ignore[arg-type]
        shot_store,
    )
    return service, scene_store, shot_store, writer


# --------------------------------------------------------------------------- #
# SC-5 归档前置条件
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_archiving_scene_with_in_production_shot_is_denied() -> None:
    scene = _scene()
    shots = _ShotStore([_shot(status=ShotStatus.IN_PRODUCTION)])
    service, store, _, outbox = _service(scenes=_SceneStore(), shots=shots)
    store.rows[scene.entity_id] = scene

    with pytest.raises(DomainError) as exc:
        await service.change_status(
            entity_id=scene.entity_id,
            target_status=SceneStatus.ARCHIVED,
            changed_by="user-1",
            tenant_id=_TENANT,
        )

    assert exc.value.error_code.code == "STATE_TRANSITION_DENIED"
    assert "IN_PRODUCTION" in exc.value.detail
    # 拒绝必须是「没发生」：状态没改、没落库、没写事实。
    assert store.rows[scene.entity_id].status is SceneStatus.DRAFT
    assert store.saved == []
    assert outbox.transitions == []


@pytest.mark.asyncio
async def test_archiving_scene_without_in_production_shot_succeeds() -> None:
    scene = _scene()
    shots = _ShotStore([_shot(status=ShotStatus.PRODUCTION_COMPLETE)])
    service, store, _, outbox = _service(scenes=_SceneStore(), shots=shots)
    store.rows[scene.entity_id] = scene

    await service.change_status(
        entity_id=scene.entity_id,
        target_status=SceneStatus.ARCHIVED,
        changed_by="user-1",
        tenant_id=_TENANT,
    )

    assert store.rows[scene.entity_id].status is SceneStatus.ARCHIVED
    assert len(outbox.transitions) == 1
    assert outbox.transitions[0].to_status == "ARCHIVED"


@pytest.mark.asyncio
async def test_non_archive_transition_does_not_query_shots() -> None:
    """守卫只在归档时求值——否则每次状态变更都多一次全表存在性查询。"""
    scene = _scene()
    shots = _ShotStore([_shot(status=ShotStatus.IN_PRODUCTION)])
    service, store, _, _ = _service(scenes=_SceneStore(), shots=shots)
    store.rows[scene.entity_id] = scene

    await service.change_status(
        entity_id=scene.entity_id,
        target_status=SceneStatus.ACTIVE,
        changed_by="user-1",
        tenant_id=_TENANT,
    )

    assert shots.queries == []
    assert store.rows[scene.entity_id].status is SceneStatus.ACTIVE


# --------------------------------------------------------------------------- #
# SC-3 编号唯一域（项目内）
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_same_scene_code_in_another_episode_is_rejected_as_occupied() -> None:
    """跨集同名必须被拒：契约 SC-3 是**项目内**唯一，不是集内唯一。"""
    service, store, _, outbox = _service(
        episodes=[_episode("proj-1", "ep-1"), _episode("proj-1", "ep-2")]
    )
    await service.create_scene(
        project_id="proj-1",
        episode_id="ep-1",
        scene_code="S01",
        created_by="user-1",
        tenant_id=_TENANT,
        entity_id="scene-1",
    )

    with pytest.raises(DomainError) as exc:
        await service.create_scene(
            project_id="proj-1",
            episode_id="ep-2",
            scene_code="S01",
            created_by="user-1",
            tenant_id=_TENANT,
        )

    assert exc.value.error_code.code == "IDEMPOTENCY_CONFLICT"
    # 关键：报的是「编号被占用」，不是「幂等键复用」。
    assert "项目内已被占用" in exc.value.detail
    assert len(store.rows) == 1
    assert [e[1] for e in outbox.events] == ["SCENE_CREATED"]


@pytest.mark.asyncio
async def test_idempotent_create_returns_existing_scene() -> None:
    """同 entity_id 同内容重复登记 → 回读原对象，不新增、不发第二条事件。"""
    service, store, _, outbox = _service()
    first = await service.create_scene(
        project_id="proj-1",
        episode_id="ep-1",
        scene_code="S01",
        created_by="user-1",
        tenant_id=_TENANT,
        entity_id="scene-1",
    )
    second = await service.create_scene(
        project_id="proj-1",
        episode_id="ep-1",
        scene_code="S01",
        created_by="user-1",
        tenant_id=_TENANT,
        entity_id="scene-1",
    )

    assert first.entity_id == second.entity_id
    assert len(store.rows) == 1
    assert len(outbox.events) == 1
