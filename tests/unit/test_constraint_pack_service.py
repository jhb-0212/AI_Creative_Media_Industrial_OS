"""ConstraintPackService 的用例（A23）。

这个文件盯的不是「能建包」，而是四条**顺序与归属**上的判断，它们各自的错法都
不会在功能上显形：

1. 幂等回读必须早于归属校验——否则重放一个当时成功过的请求，会在镜头被下游
   改名/删除后变成 4xx，调用方永远无法确认自己那次创建到底有没有生效。
2. 越界镜头要给可读的 400 并点出 id——库层触发器也能挡（23503），但它只会说
   「引用关系不成立」，调用方不知道该改哪个 id。
3. 锁定必须落齐「审批人 / 审批时间 / 依据 / 事实」四要素——少写任何一项，
   库里仍然是一行合法的 ACTIVE，只是「谁批准的」成了空白。
4. 同一逻辑包已有生效版本时，锁不上必须是**可读的 409 并点出占用者**，
   而不是 500，也不是静默双 ACTIVE。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from packages.errors.error_codes import DomainError
from packages.shared.port_errors import DuplicateEntityError
from packages.shared.status_transition import TransitionEvent, TransitionFact
from services.project_planning.domain.constraint_pack import ConstraintPack
from services.project_planning.domain.enums import ConstraintPackStatus
from services.project_planning.domain.project import Project
from services.project_planning.repository.project_repo import ProjectRepository
from services.project_planning.repository.shot_planning_repositories import (
    ConstraintPackRepository,
    ShotRepository,
)
from services.project_planning.service.constraint_pack_service import ConstraintPackService

_TENANT = uuid4()
_ENTRY: dict[str, Any] = {
    "constraint_id": "c-1",
    "target": "character.wardrobe",
    "shot_scope": "shot-1",
    "rule": "外套保持深灰",
    "source": "导演确认",
    "authority_level": "HIGH",
    "valid_at": "S01",
    "approval_ref": "apr-1",
}


class _Packs(ConstraintPackRepository):
    """内存约束包仓储。

    ``add_error`` / ``save_error`` 是刻意的故障注入点：本服务有三条分支只在
    驱动层唯一冲突时才可达，没有注入就无法在单测里走到它们——而它们恰恰是
    用户真正会看到的那几句话。
    """

    def __init__(self) -> None:
        self.rows: dict[str, ConstraintPack] = {}
        self.added: list[str] = []
        self.saved: list[tuple[str, int]] = []
        self.add_error: Exception | None = None
        self.save_error: Exception | None = None

    async def add(self, pack: ConstraintPack) -> None:
        if self.add_error is not None:
            raise self.add_error
        self.added.append(pack.entity_id)
        self.rows[pack.entity_id] = pack

    async def save(self, pack: ConstraintPack, expected_version: int) -> None:
        if self.save_error is not None:
            raise self.save_error
        self.saved.append((pack.entity_id, expected_version))
        self.rows[pack.entity_id] = pack

    async def get_by_id(self, entity_id: str) -> ConstraintPack | None:
        return self.rows.get(entity_id)

    async def get_active_by_pack(self, constraint_pack_id: str) -> ConstraintPack | None:
        for pack in self.rows.values():
            if (
                pack.constraint_pack_id == constraint_pack_id
                and pack.status is ConstraintPackStatus.ACTIVE
            ):
                return pack
        return None

    async def get_by_pack_version(
        self, constraint_pack_id: str, version: int
    ) -> ConstraintPack | None:
        for pack in self.rows.values():
            if pack.constraint_pack_id == constraint_pack_id and pack.version == version:
                return pack
        return None


class _Shots(ShotRepository):
    """只实现归属查询；其余方法一律炸——本服务不该碰镜头内容。"""

    def __init__(self, known: set[str] | None = None) -> None:
        self.known = known if known is not None else {"shot-1"}
        self.queried: list[tuple[str, list[str]]] = []

    async def existing_ids_in_project(self, project_id: str, shot_ids: list[str]) -> set[str]:
        self.queried.append((project_id, list(shot_ids)))
        return self.known & set(shot_ids)

    async def add(self, shot: Any) -> None:
        raise AssertionError("约束包服务不写镜头")

    async def save(self, shot: Any, expected_version: int) -> None:
        raise AssertionError("约束包服务不写镜头")

    async def get_by_id(self, entity_id: str) -> Any:
        raise AssertionError("约束包服务不读镜头内容")

    async def get_by_project_code(self, project_id: str, shot_code: str) -> Any:
        raise AssertionError("约束包服务不读镜头内容")

    async def has_with_status(self, scene_id: str, status: Any) -> bool:
        raise AssertionError("约束包服务不查子对象")


class _Projects(ProjectRepository):
    def __init__(self, known: set[str] | None = None) -> None:
        self.known = known if known is not None else {"proj-1"}

    async def add(self, project: Project) -> None:
        raise AssertionError("约束包服务不建项目")

    async def save(self, project: Project, expected_version: int) -> None:
        raise AssertionError("约束包服务不改项目")

    async def get_by_id(self, entity_id: str) -> Project | None:
        if entity_id not in self.known:
            return None
        return Project(
            entity_id=entity_id,
            project_name="测试项目",
            created_by="user-1",
            tenant_id=_TENANT,
            organization_id=uuid4(),
        )

    async def get_by_id_for_child_write(self, entity_id: str) -> Project | None:
        raise AssertionError("约束包不走项目子对象写锁：包不改变项目状态")


@dataclass
class _Outbox:
    # 两条通道刻意分开：``record`` 是普通领域事件，``record_transition`` 是
    # 「状态迁移事实 + 通知事件」的成对写入。混成一个列表会让「锁定到底写了
    # 事实没有」这种断言失去分辨力。
    events: list[tuple[str, str]] = field(default_factory=list)
    transitions: list[TransitionFact] = field(default_factory=list)
    transition_events: list[str] = field(default_factory=list)

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
        self.transition_events.append(event.event_type)
        return uuid4()


def _service(
    *, packs: _Packs | None = None, shots: _Shots | None = None
) -> tuple[ConstraintPackService, _Packs, _Shots, _Outbox]:
    packs = packs or _Packs()
    shots = shots or _Shots()
    outbox = _Outbox()
    service = ConstraintPackService(packs, shots, _Projects(), outbox)  # type: ignore[arg-type]
    return service, packs, shots, outbox


async def _create(service: ConstraintPackService, **overrides: Any) -> ConstraintPack:
    params: dict[str, Any] = {
        "project_id": "proj-1",
        "shot_ids": ["shot-1"],
        "applicability": "APPLICABLE",
        "created_by": "director-1",
        "tenant_id": _TENANT,
        "locked_dimensions": ["cut_points"],
        "constraints": [dict(_ENTRY)],
    }
    params.update(overrides)
    return await service.create_pack(**params)


# --------------------------------------------------------------------------- #
# 创建：顺序与归属
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_idempotent_replay_precedes_shot_ownership_check() -> None:
    """重放必须回读原包，且**不能**再查一次镜头归属。

    查了就会有两个坏结果：镜头已改名的重试被判成 4xx（调用方无法确认创建是否
    生效），以及多一次无意义的归属查询。
    """
    service, packs, shots, outbox = _service()
    first = await _create(service, entity_id="pack-1")
    shots.queried.clear()
    outbox.events.clear()

    replay = await _create(service, entity_id="pack-1")

    assert replay.entity_id == first.entity_id
    assert shots.queried == [], "幂等回读命中后不得再查归属"
    assert outbox.events == [], "重放不是新建，不能再发 CREATED"
    assert packs.added == ["pack-1"]


@pytest.mark.asyncio
async def test_replay_with_different_content_is_idempotency_conflict() -> None:
    service, _, shots, _ = _service()
    await _create(service, entity_id="pack-1", version=1)
    shots.queried.clear()

    with pytest.raises(DomainError) as exc:
        await _create(service, entity_id="pack-1", version=2)

    assert exc.value.error_code.code == "IDEMPOTENCY_CONFLICT"
    assert shots.queried == []


@pytest.mark.asyncio
async def test_replay_with_different_pack_id_is_conflict() -> None:
    """包标识是业务字段：同一 entity_id 换一个包标识，不是同一个请求。"""
    service, _, _, _ = _service()
    await _create(service, entity_id="pack-1")

    with pytest.raises(DomainError) as exc:
        await _create(service, entity_id="pack-1", constraint_pack_id="cp-other")

    assert exc.value.error_code.code == "IDEMPOTENCY_CONFLICT"


@pytest.mark.asyncio
async def test_missing_project_is_not_found_before_shot_lookup() -> None:
    """父资源不存在时先给 404：此时谈「这些镜头不属于该项目」是误导。"""
    service, packs, shots, _ = _service()

    with pytest.raises(DomainError) as exc:
        await _create(service, project_id="proj-unknown")

    assert exc.value.error_code.code == "NOT_FOUND"
    assert shots.queried == []
    assert packs.added == []


@pytest.mark.asyncio
async def test_cross_project_shots_are_rejected_with_ids_named() -> None:
    """越界镜头给 400 并点出 id——那是调用方唯一能照着改的信息。"""
    service, packs, _, outbox = _service(shots=_Shots(known={"shot-1"}))

    with pytest.raises(DomainError) as exc:
        await _create(service, shot_ids=["shot-1", "shot-9", "shot-7"])

    assert exc.value.error_code.code == "VALIDATION_ERROR"
    assert "shot-7" in exc.value.detail and "shot-9" in exc.value.detail
    assert packs.added == []
    assert outbox.events == []


@pytest.mark.asyncio
async def test_created_event_carries_pack_shape_not_entries() -> None:
    """事件只带包的形状（计数/适用性/版本），不带条目正文。"""
    service, _, _, outbox = _service()

    await _create(service, entity_id="pack-1")

    assert outbox.events == [("ConstraintPack", "CONSTRAINT_PACK_CREATED")]
    assert outbox.transitions == [], "创建不是状态推进，不该有迁移事实"


@pytest.mark.asyncio
async def test_duplicate_version_conflict_names_the_occupant() -> None:
    """同包同版本被占：必须说清「被哪个实体占着」，否则不知道该动哪个参数。"""
    service, packs, _, _ = _service()
    await _create(service, entity_id="pack-1", constraint_pack_id="cp-1", version=1)
    packs.add_error = DuplicateEntityError("pack-1", constraint="uq_constraint_pack_version")

    with pytest.raises(DomainError) as exc:
        await _create(service, entity_id="pack-2", constraint_pack_id="cp-1", version=1)

    assert exc.value.error_code.code == "IDEMPOTENCY_CONFLICT"
    assert "pack-1" in exc.value.detail, "必须点出占用者"
    assert "version" in exc.value.detail, "必须给出修复方向"


@pytest.mark.asyncio
async def test_blank_project_id_is_rejected_before_any_io() -> None:
    service, packs, shots, _ = _service()

    with pytest.raises(DomainError) as exc:
        await _create(service, project_id="   ")

    assert exc.value.error_code.code == "VALIDATION_ERROR"
    assert shots.queried == [] and packs.added == []


# --------------------------------------------------------------------------- #
# 生效：四要素
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_lock_requires_approver_and_basis() -> None:
    """审批人 / 依据缺一不可，且拒绝时状态与事件都不该有任何变化。"""
    service, packs, _, outbox = _service()
    pack = await _create(service, entity_id="pack-1")
    stamp = datetime(2026, 11, 21, tzinfo=UTC)

    with pytest.raises(DomainError) as no_actor:
        await service.lock_pack(
            entity_id=pack.entity_id,
            approved_by="  ",
            approved_at=stamp,
            lock_basis_ref="apr-1",
            tenant_id=_TENANT,
        )
    with pytest.raises(DomainError) as no_basis:
        await service.lock_pack(
            entity_id=pack.entity_id,
            approved_by="director-1",
            approved_at=stamp,
            lock_basis_ref="   ",
            tenant_id=_TENANT,
        )

    assert "approved_by" in no_actor.value.detail
    assert "lock_basis_ref" in no_basis.value.detail
    assert pack.status is ConstraintPackStatus.DRAFT
    assert packs.saved == [] and outbox.transitions == []


@pytest.mark.asyncio
async def test_lock_records_approval_fields_and_transition_fact() -> None:
    """四要素一次落齐：业务字段 + 迁移事实（含 owner_action 与 authority_ref）。"""
    service, packs, _, outbox = _service()
    pack = await _create(service, entity_id="pack-1")
    outbox.events.clear()
    stamp = datetime(2026, 11, 21, 9, 30, tzinfo=UTC)

    locked = await service.lock_pack(
        entity_id=pack.entity_id,
        approved_by="director-1",
        approved_at=stamp,
        lock_basis_ref="apr-1",
        tenant_id=_TENANT,
    )

    assert locked.status is ConstraintPackStatus.ACTIVE
    assert locked.approved_by == "director-1"
    assert locked.approved_at == stamp
    assert packs.saved == [("pack-1", 1)]

    assert len(outbox.transitions) == 1
    fact = outbox.transitions[0]
    assert fact.resource_type == "ConstraintPack"
    assert fact.from_status == "DRAFT" and fact.to_status == "ACTIVE"
    # 边所有权：只要不是 LOCK 走这条边，闸门 data.transition_single_entry 会红；
    # 在这里断言是因为它是「谁有权让包生效」的唯一凭据。
    assert fact.owner_action == "CONSTRAINT.PACK.LOCK"
    assert fact.actor_subject == "director-1"
    assert fact.authority_ref == "apr-1"
    assert outbox.transition_events == ["CONSTRAINT_PACK_LOCKED"]
    assert outbox.events == [], "状态变更走 record_transition，不该再单独发一次"


@pytest.mark.asyncio
async def test_not_applicable_pack_must_also_go_through_lock() -> None:
    """「已确认无锁定约束」同样是授权判断：它也必须经 ACTIVE 才算数。"""
    service, _, _, outbox = _service()
    pack = await _create(
        service,
        entity_id="pack-1",
        applicability="NOT_APPLICABLE",
        locked_dimensions=[],
        constraints=[],
    )

    assert pack.status is ConstraintPackStatus.DRAFT
    locked = await service.lock_pack(
        entity_id=pack.entity_id,
        approved_by="director-1",
        approved_at=datetime(2026, 11, 21, tzinfo=UTC),
        lock_basis_ref="apr-1",
        tenant_id=_TENANT,
    )

    assert locked.status is ConstraintPackStatus.ACTIVE
    assert len(outbox.transitions) == 1


@pytest.mark.asyncio
async def test_locking_twice_is_denied_not_silently_idempotent() -> None:
    """已生效再锁：拒绝，且不能产生第二条迁移事实。"""
    service, packs, _, outbox = _service()
    pack = await _create(service, entity_id="pack-1")
    stamp = datetime(2026, 11, 21, tzinfo=UTC)
    await service.lock_pack(
        entity_id=pack.entity_id,
        approved_by="director-1",
        approved_at=stamp,
        lock_basis_ref="apr-1",
        tenant_id=_TENANT,
    )
    outbox.transitions.clear()
    packs.saved.clear()

    with pytest.raises(DomainError) as exc:
        await service.lock_pack(
            entity_id=pack.entity_id,
            approved_by="director-2",
            approved_at=stamp,
            lock_basis_ref="apr-2",
            tenant_id=_TENANT,
        )

    assert exc.value.error_code.code == "STATE_TRANSITION_DENIED"
    assert "不得原地重锁" in exc.value.detail
    assert outbox.transitions == [] and packs.saved == []


@pytest.mark.asyncio
async def test_single_active_conflict_explains_missing_supersede_entry() -> None:
    """部分唯一索引冲突 = 版本推进没有入口，不是「重复提交」。

    这两种原因的用户动作完全不同：前者要找治理动作取代旧版本，后者只需重试。
    把后者的话术用在前者上，调用方会一直重试同一条注定失败的请求。
    """
    service, packs, _, _ = _service()
    v1 = await _create(service, entity_id="pack-1")
    v2 = await _create(service, entity_id="pack-2", constraint_pack_id="pack-1", version=2)
    assert v2.constraint_pack_id == v1.constraint_pack_id, "两版本共用同一逻辑包标识"

    packs.save_error = DuplicateEntityError("pack-2", constraint="uq_constraint_pack_single_active")

    with pytest.raises(DomainError) as exc:
        await service.lock_pack(
            entity_id=v2.entity_id,
            approved_by="director-1",
            approved_at=datetime(2026, 11, 21, tzinfo=UTC),
            lock_basis_ref="apr-2",
            tenant_id=_TENANT,
        )

    assert exc.value.error_code.code == "IDEMPOTENCY_CONFLICT"
    assert "CONSTRAINT.PACK.STATUS.CHANGE" in exc.value.detail
    assert v2.status is ConstraintPackStatus.DRAFT, "拒绝后不得留在 ACTIVE"


@pytest.mark.asyncio
async def test_get_pack_missing_is_not_found() -> None:
    service, _, _, _ = _service()

    with pytest.raises(DomainError) as exc:
        await service.get_pack("pack-unknown")

    assert exc.value.error_code.code == "NOT_FOUND"
