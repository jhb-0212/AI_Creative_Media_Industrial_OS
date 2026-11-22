"""拆镜确认路径的唯一冲突映射（ADR-R28 §6.4）。

为什么这条路径需要专门的用例
------------------------------------------------------------
本服务是全项目唯一**批量**写入镜头编号的路径，而编号是用户可见的业务键：
「编号重复」是常态，不是异常。端口层对所有唯一冲突只给一个异常
（``DuplicateEntityError``），若不翻译，它会冒到 API 层变成 500——那等于告诉
调用方「服务端坏了，重试吧」，而真正的修法在调用方手里（换编号 / 修订草案）。

三条判据（各自对应一个真实的 23505 来源）：

1. 草案内编号重复（同一次确认里两个镜头用同一个 ``shot_code``）；
2. 项目内编号已被别的镜头占用（``uq_shot_code_per_project``）；
3. 同一镜头已有 ACTIVE 修订（``ux_shot_active_revision``）→ 并发，可重试。

前两条的**修复方向相反**，所以错误详情必须点出占用者是谁；第三条是可重试的
并发冲突。三者都是 409，但都不能是 500。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID, uuid4

import pytest

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.shared.port_errors import DuplicateEntityError
from packages.shared.status_transition import TransitionEvent, TransitionFact
from services.project_planning.domain.enums import SceneStatus, ShotDraftStatus
from services.project_planning.domain.project import Project
from services.project_planning.domain.scene import Scene
from services.project_planning.domain.shot import Shot
from services.project_planning.domain.shot_draft import ShotBreakdownDraft
from services.project_planning.domain.shot_revision import ShotRevision
from services.project_planning.repository.project_repo import ProjectRepository
from services.project_planning.repository.shot_planning_repositories import (
    SceneRepository,
    ShotDraftRepository,
    ShotRepository,
    ShotRevisionRepository,
)
from services.project_planning.service.shot_draft_service import ShotDraftService

_TENANT = uuid4()

_SPEC: dict[str, object] = {
    "shot_code": "S01-01",
    "description": "开场推轨",
    "narrative_intent": "建立空间",
    "character_refs": [],
    "location_ref": "loc-1",
    "prop_refs": [],
    "constraint_pack_applicability": "NONE",
    "creative_autonomy": "SEMI_AUTO",
    "risk": {},
    "quality_profile": "QP_STANDARD",
    "intent_ref": {},
    "script_anchors": [],
    "change_origin": "INITIAL",
}


class _Drafts(ShotDraftRepository):
    def __init__(self, draft: ShotBreakdownDraft) -> None:
        self.rows: dict[str, ShotBreakdownDraft] = {draft.entity_id: draft}

    async def add(self, draft: ShotBreakdownDraft) -> None:
        if draft.entity_id in self.rows:
            raise DuplicateEntityError(draft.entity_id)
        self.rows[draft.entity_id] = draft

    async def save(self, draft: ShotBreakdownDraft, expected_version: int) -> None:
        self.rows[draft.entity_id] = draft

    async def get_by_id(self, entity_id: str) -> ShotBreakdownDraft | None:
        return self.rows.get(entity_id)


class _Shots(ShotRepository):
    """内存镜头仓储：按 (project_id, shot_code) 拒绝重复——模拟库层唯一约束。

    刻意不提供「开关式拒绝」：那会让用例测到替身的配置，而不是测到服务层对**真实
    约束行为**的解释。约束的形状就是「同项目同编号只能有一行」，重复提交第二次
    才会撞上。
    """

    def __init__(self) -> None:
        self.rows: dict[str, Shot] = {}

    async def existing_ids_in_project(
        self, project_id: str, shot_ids: list[str]
    ) -> set[str]:
        # 本文件不建约束包：镜头归属查询只服务 CONSTRAINT.PACK.CREATE。
        # 让它直接失败，是为了把「别处误用这个端口」变成红灯，而不是返回一个
        # 看起来合理的空集（空集会让「镜头都不属于本项目」变成静默通过）。
        raise AssertionError("本用例不涉及约束包，不应查询镜头归属")

    async def add(self, shot: Shot) -> None:
        if await self.get_by_project_code(shot.project_id, shot.shot_code) is not None:
            raise DuplicateEntityError(shot.entity_id, constraint="uq_shot_code_per_project")
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

    async def has_with_status(self, scene_id: str, status: object) -> bool:
        raise AssertionError("确认路径不查子对象状态")


class _Revisions(ShotRevisionRepository):
    def __init__(self, *, reject: bool = False) -> None:
        self.rows: dict[str, ShotRevision] = {}
        self.reject = reject

    async def add(self, revision: ShotRevision) -> None:
        if self.reject:
            raise DuplicateEntityError(revision.entity_id, constraint="ux_shot_active_revision")
        self.rows[revision.entity_id] = revision

    async def save(self, revision: ShotRevision, expected_version: int) -> None:
        self.rows[revision.entity_id] = revision

    async def get_by_id(self, entity_id: str) -> ShotRevision | None:
        return self.rows.get(entity_id)

    async def next_revision_no(self, shot_id: str) -> int:
        return 1

    async def get_active_by_shot(self, shot_id: str) -> ShotRevision | None:
        return None

    async def lock_for_child_write(self, entity_id: str) -> ShotRevision | None:
        # 确认路径不建规格，故不需要锁；实现它只为满足端口（A22 新增）。
        raise AssertionError("确认路径不取修订共享锁")


class _Scenes(SceneRepository):
    def __init__(self, scenes: list[Scene]) -> None:
        self.rows: dict[str, Scene] = {s.entity_id: s for s in scenes}
        # 记录取锁顺序：「多场次按全序加锁」是防死锁的唯一依据，必须可断言。
        self.lock_order: list[str] = []

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
        raise AssertionError("确认路径不归档场次")

    async def lock_for_child_write(self, entity_id: str) -> Scene | None:
        # 内存夹具无行锁语义，只记录调用顺序；真实仓储实现为 FOR SHARE。
        self.lock_order.append(entity_id)
        return self.rows.get(entity_id)


class _Projects(ProjectRepository):
    def __init__(self) -> None:
        self.rows: dict[str, Project] = {}

    async def add(self, project: Project) -> None:
        self.rows[project.entity_id] = project

    async def save(self, project: Project, expected_version: int) -> None:
        self.rows[project.entity_id] = project

    async def get_by_id(self, entity_id: str) -> Project | None:
        return self.rows.get(entity_id)

    async def get_by_id_for_child_write(self, entity_id: str) -> Project | None:
        # 内存夹具无行锁语义，等同普通读（真实 SQLAlchemy 仓储实现为 FOR SHARE）。
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


def _draft(*shot_specs: dict[str, object]) -> ShotBreakdownDraft:
    return ShotBreakdownDraft(
        entity_id="draft-1",
        project_id="proj-1",
        basis_script_revision_id="script-1",
        plan_name="第一版",
        generation_method="AI",
        scene_breakdowns=[{"scene_id": "scene-1", "shots": list(shot_specs) or [_SPEC]}],
        breakdown_scope={"episode_ref": "E01"},
        root_draft_ref="draft-1",
        created_by="user-1",
        tenant_id=_TENANT,
    )


def _scene(
    *, status: SceneStatus = SceneStatus.DRAFT, entity_id: str = "scene-1"
) -> Scene:
    return Scene(
        entity_id=entity_id,
        project_id="proj-1",
        episode_id="ep-1",
        scene_code="S01",
        created_by="user-1",
        tenant_id=_TENANT,
        status=status,
    )


def _service(
    draft: ShotBreakdownDraft,
    *,
    shots: _Shots | None = None,
    revisions: _Revisions | None = None,
    scenes: _Scenes | None = None,
) -> tuple[ShotDraftService, _Shots, _Revisions, _Outbox]:
    shot_store = shots or _Shots()
    revision_store = revisions or _Revisions()
    outbox = _Outbox()
    service = ShotDraftService(
        _Drafts(draft),
        shot_store,
        revision_store,
        scenes if scenes is not None else _Scenes([_scene()]),
        _Projects(),
        outbox,
    )
    return service, shot_store, revision_store, outbox


@pytest.mark.asyncio
async def test_code_repeated_inside_the_draft_is_explained_not_500() -> None:
    draft = _draft(_SPEC, _SPEC)
    service, _, _, _ = _service(draft)

    with pytest.raises(DomainError) as exc:
        await service.confirm_draft(
            entity_id=draft.entity_id, confirmed_by="user-1", tenant_id=_TENANT
        )

    assert exc.value.error_code.code == "IDEMPOTENCY_CONFLICT"
    assert exc.value.error_code.http_status == 409
    assert "本草案内编号重复" in exc.value.detail, exc.value.detail


@pytest.mark.asyncio
async def test_code_taken_by_another_shot_names_the_occupant() -> None:
    """占用者来自别的草案/历史镜头时，详情必须指出它，否则调用方无从修起。"""
    draft = _draft()
    shots = _Shots()
    # 库里已有一条同编号的镜头（另一个草案确认产出）。
    shots.rows["shot-old"] = Shot(
        entity_id="shot-old",
        project_id="proj-1",
        scene_id="scene-9",
        shot_code="S01-01",
        shot_revision_id="rev-old",
        created_by="user-0",
        tenant_id=_TENANT,
    )
    service, _, _, _ = _service(draft, shots=shots)

    with pytest.raises(DomainError) as exc:
        await service.confirm_draft(
            entity_id=draft.entity_id, confirmed_by="user-1", tenant_id=_TENANT
        )

    assert exc.value.error_code.code == "IDEMPOTENCY_CONFLICT"
    assert "scene_id=scene-9" in exc.value.detail
    assert "修订草案" in exc.value.detail


@pytest.mark.asyncio
async def test_active_revision_conflict_is_retryable_concurrency() -> None:
    """同一镜头已有 ACTIVE 修订 → 并发确认，语义是可重试的 409。"""
    draft = _draft()
    service, _, _, _ = _service(draft, revisions=_Revisions(reject=True))

    with pytest.raises(DomainError) as exc:
        await service.confirm_draft(
            entity_id=draft.entity_id, confirmed_by="user-1", tenant_id=_TENANT
        )

    assert exc.value.error_code.code == "CONCURRENT_MODIFICATION"
    assert exc.value.error_code.http_status == 409
    assert exc.value.error_code.retryable is True


@pytest.mark.asyncio
async def test_happy_path_still_confirms_with_two_transition_facts() -> None:
    """映射改动不得改变成功路径：认领 + 落定两条事实、一个 Shot、一个修订。"""
    draft = _draft()
    service, shots, revisions, outbox = _service(draft)

    confirmed = await service.confirm_draft(
        entity_id=draft.entity_id, confirmed_by="user-1", tenant_id=_TENANT
    )

    assert confirmed.status is ShotDraftStatus.CONFIRMED
    assert len(shots.rows) == 1
    assert len(revisions.rows) == 1
    assert [f.to_status for f in outbox.transitions] == ["IN_REVIEW", "CONFIRMED"]


# --------------------------------------------------------------------------- #
# 父场次冻结：确认拆镜不得在归档场次下新增镜头
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_confirm_rejects_archived_parent_scene() -> None:
    """归档场次下不得新增镜头（与「归档前不得有在产镜头」是同一规则的两侧）。

    漏掉这一侧的后果不是报错，而是**静默长出僵尸镜头**：新镜头以 NOT_STARTED
    出生、永远进不了在产（父场次屏障挡住），但会被进度与预算统计当成真实对象。
    """
    scenes = _Scenes([_scene(status=SceneStatus.ARCHIVED)])
    service, shots, _, outbox = _service(_draft(), scenes=scenes)

    with pytest.raises(DomainError) as exc:
        await service.confirm_draft(
            entity_id="draft-1", confirmed_by="user-1", tenant_id=_TENANT
        )

    assert exc.value.error_code is ErrorRegistry.PARENT_LIFECYCLE_FROZEN
    assert shots.rows == {}, "拒绝必须发生在 INSERT 之前"
    assert outbox.events == []


@pytest.mark.asyncio
async def test_confirm_locks_all_parent_scenes_in_sorted_order() -> None:
    """多场次草案按 scene_id 全序取锁——环只可能由顺序不一致产生。"""
    draft = _draft()
    draft.scene_breakdowns = [
        {"scene_id": "scene-b", "shots": [{**_SPEC, "shot_code": "S02-01"}]},
        {"scene_id": "scene-a", "shots": [{**_SPEC, "shot_code": "S01-01"}]},
    ]
    scenes = _Scenes(
        [
            _scene(entity_id="scene-a"),
            _scene(entity_id="scene-b"),
        ]
    )
    service, _, _, _ = _service(draft, scenes=scenes)

    await service.confirm_draft(
        entity_id="draft-1", confirmed_by="user-1", tenant_id=_TENANT
    )

    assert scenes.lock_order == ["scene-a", "scene-b"], (
        "取锁顺序必须与草案里的出现顺序无关：跟随调用方顺序会让两个确认事务"
        "以相反次序取锁而死锁"
    )
