"""ShotSpecService 的创建与锁定用例（A22）。

本文件盯的是**顺序与证据**，不是「抛没抛异常」：
- 创建侧的幂等判定必须排在父链锁读与冻结判定之前——否则修订被取代后的重试
  会变成 4xx，调用方永远无法确认自己那次创建到底有没有生效；
- 锁定侧必须经 ``advance_status`` 留下迁移事实（``authority_ref`` = 锁定依据），
  只断言「状态变了」会让「状态改了、审计查不到」的实现通过。

并发正确性（唯一约束、共享锁、乐观锁）不在这里假装验证：它们由库层约束与
``tests/integration`` 的真实 PG 用例证明，内存替身单线程跑不出竞态。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from packages.errors.error_codes import DomainError
from packages.shared.port_errors import (
    DuplicateEntityError,
    ReferentialIntegrityError,
)
from packages.shared.status_transition import TransitionEvent, TransitionFact
from services.project_planning.domain.enums import ShotRevisionStatus, ShotSpecStatus
from services.project_planning.domain.shot_revision import ShotRevision
from services.project_planning.domain.shot_spec import ShotSpec
from services.project_planning.repository.project_repo import ProjectRepository
from services.project_planning.repository.shot_planning_repositories import (
    ShotRevisionRepository,
    ShotSpecRepository,
)
from services.project_planning.service.shot_spec_service import ShotSpecService

_TENANT = uuid4()


@dataclass
class _RevisionView:
    """规格服务对修订的全部依赖面：``project_id`` 与 ``status``。

    刻意不用真实的 ``ShotRevision`` 聚合：它的构造要求十几个与本用例无关的
    必填字段，耦合进来只会让「归属判定」这一个判据被无关字段校验的噪音淹没。
    替身与服务的依赖面一样窄，就说明服务确实只依赖这两件事。
    """

    project_id: str = "proj-1"
    status: ShotRevisionStatus = ShotRevisionStatus.ACTIVE


class _Revisions(ShotRevisionRepository):
    """只实现 ``lock_for_child_write``；其余方法一旦被调用即失败。

    这不是「省事」：服务创建规格时若改去用 ``get_by_id`` 读修订，就会丢掉
    共享锁（并发窗口重新出现）。让其它方法直接断言失败，能把这种退化变成
    测试红灯，而不是一个只在生产并发下才显现的漏洞。
    """

    def __init__(self, revision: _RevisionView | None = None) -> None:
        self.revision = revision
        self.locked: list[str] = []

    async def add(self, revision: Any) -> None:
        raise AssertionError("创建规格不该写修订")

    async def save(self, revision: Any, expected_version: int) -> None:
        raise AssertionError("创建规格不该写修订")

    async def get_by_id(self, entity_id: str) -> Any:
        raise AssertionError("必须走 lock_for_child_write：无锁读会放过并发取代")

    async def next_revision_no(self, shot_id: str) -> int:
        raise AssertionError("创建规格不涉及修订编号")

    async def get_active_by_shot(self, shot_id: str) -> Any:
        raise AssertionError("创建规格不查活跃修订")

    async def lock_for_child_write(self, entity_id: str) -> ShotRevision | None:
        self.locked.append(entity_id)
        # 替身只实现服务真正用到的两个属性（project_id / status），故在此转换：
        # 构造完整 ShotRevision 需要十几个与本用例无关的必填字段。
        return cast(ShotRevision, self.revision)


class _Projects(ProjectRepository):
    def __init__(self, exists: bool = True) -> None:
        self.exists = exists
        self.calls: list[str] = []

    async def add(self, project: Any) -> None:
        raise AssertionError("创建规格不该写项目")

    async def save(self, project: Any, expected_version: int) -> None:
        raise AssertionError("创建规格不该写项目")

    async def get_by_id(self, entity_id: str) -> Any:
        self.calls.append(entity_id)
        return object() if self.exists else None

    async def get_by_id_for_child_write(self, entity_id: str) -> Any:
        raise AssertionError("规格的父链屏障在修订上，项目只需存在性")


class _Specs(ShotSpecRepository):
    """内存规格仓储：可注入写失败，用于验证端口失败契约的翻译。"""

    def __init__(self, seed: list[ShotSpec] | None = None) -> None:
        self.rows: dict[str, ShotSpec] = {s.entity_id: s for s in seed or []}
        self.added: list[ShotSpec] = []
        self.saved: list[tuple[str, int]] = []
        self.add_error: Exception | None = None

    async def add(self, spec: ShotSpec) -> None:
        if self.add_error is not None:
            raise self.add_error
        if spec.entity_id in self.rows:
            raise DuplicateEntityError(spec.entity_id, constraint="pk_shot_specs")
        self.added.append(spec)
        self.rows[spec.entity_id] = spec

    async def save(self, spec: ShotSpec, expected_version: int) -> None:
        self.saved.append((spec.entity_id, expected_version))
        self.rows[spec.entity_id] = spec

    async def get_by_id(self, entity_id: str) -> ShotSpec | None:
        return self.rows.get(entity_id)

    async def get_by_revision(self, shot_revision_id: str) -> ShotSpec | None:
        for spec in self.rows.values():
            if spec.shot_revision_id == shot_revision_id:
                return spec
        return None


@dataclass
class _Outbox:
    records: list[dict[str, Any]] = field(default_factory=list)
    transitions: list[tuple[TransitionFact, TransitionEvent]] = field(
        default_factory=list
    )

    def record(
        self,
        *,
        aggregate_type: str,
        aggregate_id: str,
        tenant_id: UUID,
        event_type: str,
        payload: dict[str, Any],
    ) -> UUID:
        self.records.append(
            {
                "aggregate_type": aggregate_type,
                "aggregate_id": aggregate_id,
                "event_type": event_type,
                "payload": payload,
            }
        )
        return uuid4()

    def record_transition(self, *, fact: TransitionFact, event: TransitionEvent) -> UUID:
        self.transitions.append((fact, event))
        return uuid4()


def _spec(
    *,
    entity_id: str = "spec-1",
    revision_id: str = "rev-1",
    status: ShotSpecStatus = ShotSpecStatus.DRAFT,
    locked_dimensions: str = "",
) -> ShotSpec:
    return ShotSpec(
        entity_id=entity_id,
        project_id="proj-1",
        shot_revision_id=revision_id,
        duration_ms=1000,
        fps_num=25,
        fps_den=1,
        cut_points_frames=[0, 12],
        aspect_ratio="16:9",
        coordinate_system="SCENE_LOCAL",
        camera_path="static",
        subject_paths="A",
        shot_size_changes="",
        previz_ref="previz://shot-1/v1",
        locked_dimensions=locked_dimensions,
        status=status,
        created_by="director",
        tenant_id=_TENANT,
    )


def _service(
    *,
    specs: _Specs | None = None,
    revision: _RevisionView | None = None,
    project_exists: bool = True,
) -> tuple[ShotSpecService, _Specs, _Revisions, _Projects, _Outbox]:
    specs = specs or _Specs()
    revisions = _Revisions(revision if revision is not None else _RevisionView())
    projects = _Projects(exists=project_exists)
    outbox = _Outbox()
    service = ShotSpecService(specs, revisions, projects, outbox)  # type: ignore[arg-type]
    return service, specs, revisions, projects, outbox


async def _create(
    service: ShotSpecService,
    *,
    entity_id: str | None = None,
    project_id: str = "proj-1",
    shot_revision_id: str = "rev-1",
    cut_points_frames: list[int] | None = None,
) -> ShotSpec:
    return await service.create_spec(
        project_id=project_id,
        shot_revision_id=shot_revision_id,
        duration_ms=1000,
        fps_num=25,
        fps_den=1,
        cut_points_frames=cut_points_frames if cut_points_frames is not None else [0, 12],
        aspect_ratio="16:9",
        coordinate_system="SCENE_LOCAL",
        camera_path="static",
        subject_paths="A",
        shot_size_changes="",
        previz_ref="previz://shot-1/v1",
        control_signal_refs=["signal://focus/1"],
        created_by="director",
        tenant_id=_TENANT,
        entity_id=entity_id,
    )


# --------------------------------------------------------------------------
# 创建
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_persists_and_emits_creation_event() -> None:
    service, specs, revisions, _, outbox = _service()

    spec = await _create(service, entity_id="spec-1")

    assert spec.status is ShotSpecStatus.DRAFT
    assert spec.project_id == "proj-1"
    assert spec.shot_spec_id == "spec-1"
    assert [s.entity_id for s in specs.added] == ["spec-1"]
    assert revisions.locked == ["rev-1"], "必须持共享锁读修订后再判定"
    assert [r["event_type"] for r in outbox.records] == ["SHOTSPEC_CREATED"]
    payload = outbox.records[0]["payload"]
    assert payload["shot_spec_id"] == "spec-1"
    assert payload["shot_revision_id"] == "rev-1"
    assert payload["cut_point_count"] == 2
    assert payload["status"] == "DRAFT"
    assert payload["tenant_id"] == str(_TENANT)


@pytest.mark.asyncio
async def test_idempotent_replay_returns_existing_before_touching_parents() -> None:
    """重放当时成功过的请求是重试，不是新建——必须在父链锁读之前判定。

    这里断言项目仓储与修订锁**零调用**：若顺序反了（先锁父行、先判冻结），
    修订在两次请求之间被取代，重试就会变成 409，而调用方看到 409 只能理解为
    「创建失败」，与实际（第一次已生效）相反。
    """
    existing = _spec(entity_id="spec-1")
    service, specs, revisions, projects, outbox = _service(
        specs=_Specs(seed=[existing])
    )

    spec = await _create(service, entity_id="spec-1")

    assert spec is existing
    assert specs.added == []
    assert outbox.records == []
    assert projects.calls == [], "幂等回读必须先于项目存在性查询"
    assert revisions.locked == [], "幂等回读必须先于父行锁"


@pytest.mark.asyncio
async def test_replaying_with_different_content_is_a_conflict() -> None:
    """幂等键不能复用于另一个请求：内容不同即 409，而不是静默返回旧对象。"""
    service, _, _, _, _ = _service(specs=_Specs(seed=[_spec(entity_id="spec-1")]))

    with pytest.raises(DomainError) as exc:
        await _create(service, entity_id="spec-1", cut_points_frames=[0, 5])

    assert exc.value.error_code.code == "IDEMPOTENCY_CONFLICT"
    assert "不能复用" in exc.value.detail


@pytest.mark.asyncio
async def test_missing_project_is_reported_before_the_revision_lookup() -> None:
    service, _, revisions, _, _ = _service(project_exists=False)

    with pytest.raises(DomainError) as exc:
        await _create(service, entity_id="spec-1")

    assert exc.value.error_code.code == "NOT_FOUND"
    assert "项目不存在" in exc.value.detail
    assert revisions.locked == [], "父资源不存在时不该再去锁修订"


@pytest.mark.asyncio
async def test_missing_revision_is_not_found() -> None:
    service, specs, _, _, _ = _service()
    service._shot_revision_repository = _Revisions(None)

    with pytest.raises(DomainError) as exc:
        await _create(service, entity_id="spec-1")

    assert exc.value.error_code.code == "NOT_FOUND"
    assert "镜头修订不存在" in exc.value.detail
    assert specs.added == []


@pytest.mark.asyncio
async def test_cross_project_binding_is_rejected_with_a_readable_error() -> None:
    """库层复合外键是最后防线；服务层先给出「该改哪个参数」的错误。"""
    service, specs, _, _, outbox = _service(
        revision=_RevisionView(project_id="proj-2")
    )

    with pytest.raises(DomainError) as exc:
        await _create(service, entity_id="spec-1", project_id="proj-1")

    assert exc.value.error_code.code == "VALIDATION_ERROR"
    assert "proj-2" in exc.value.detail and "proj-1" in exc.value.detail
    assert specs.added == [] and outbox.records == []


@pytest.mark.asyncio
async def test_superseded_revision_rejects_new_spec() -> None:
    """已取代的修订是冻结态，不是「暂时不可用」：其规格只作历史证据。"""
    service, specs, _, _, _ = _service(
        revision=_RevisionView(status=ShotRevisionStatus.SUPERSEDED)
    )

    with pytest.raises(DomainError) as exc:
        await _create(service, entity_id="spec-1")

    # 码与「这条边走不通」刻意分开：父已封存时重试没有意义。
    assert exc.value.error_code.code == "PARENT_LIFECYCLE_FROZEN"
    assert specs.added == []


@pytest.mark.asyncio
async def test_second_spec_for_the_same_revision_names_the_occupant() -> None:
    """同一修订至多一份规格：冲突要指出占用者，而不是笼统「重复提交」。"""
    occupant = _spec(entity_id="spec-existing", revision_id="rev-1")
    specs = _Specs(seed=[occupant])
    specs.add_error = DuplicateEntityError("spec-new", constraint="uq_shot_spec_per_revision")
    service, _, _, _, _ = _service(specs=specs)

    with pytest.raises(DomainError) as exc:
        await _create(service, entity_id="spec-new")

    assert exc.value.error_code.code == "IDEMPOTENCY_CONFLICT"
    assert "spec-existing" in exc.value.detail
    assert "新修订" in exc.value.detail


@pytest.mark.asyncio
async def test_referential_integrity_failure_is_retryable_not_idempotent() -> None:
    """父实体刚被并发删除/移出作用域：可重试的并发冲突，不可幂等返回。"""
    specs = _Specs()
    specs.add_error = ReferentialIntegrityError("spec-1", constraint="fk_shot_specs_revision")
    service, _, _, _, outbox = _service(specs=specs)

    with pytest.raises(DomainError) as exc:
        await _create(service, entity_id="spec-1")

    assert exc.value.error_code.code == "CONCURRENT_MODIFICATION"
    assert exc.value.error_code.retryable is True
    assert outbox.records == []


# --------------------------------------------------------------------------
# 锁定
# --------------------------------------------------------------------------


async def _lock(
    service: ShotSpecService,
    *,
    entity_id: str = "spec-1",
    locked_dimensions: str = "camera, subject",
    lock_basis_ref: str = "approval://board/2026-11/7",
    locked_by: str = "director",
) -> ShotSpec:
    return await service.lock_spec(
        entity_id=entity_id,
        locked_dimensions=locked_dimensions,
        lock_basis_ref=lock_basis_ref,
        locked_by=locked_by,
        tenant_id=_TENANT,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"locked_by": "  "}, "locked_by"),
        ({"lock_basis_ref": ""}, "lock_basis_ref"),
        ({"locked_dimensions": " , "}, "锁定维度"),
    ],
)
async def test_lock_requires_all_four_evidence_parts(
    kwargs: dict[str, str], expected: str
) -> None:
    specs = _Specs(seed=[_spec(entity_id="spec-1")])
    service, _, _, _, outbox = _service(specs=specs)

    with pytest.raises(DomainError) as exc:
        await _lock(service, **kwargs)

    assert exc.value.error_code.code == "VALIDATION_ERROR"
    assert expected in exc.value.detail
    assert specs.saved == [], "缺证据时不得落库"
    assert outbox.transitions == [], "缺证据时不得写迁移事实"


@pytest.mark.asyncio
async def test_lock_unknown_spec_is_not_found() -> None:
    service, _, _, _, _ = _service()

    with pytest.raises(DomainError) as exc:
        await _lock(service, entity_id="spec-missing")

    assert exc.value.error_code.code == "NOT_FOUND"


@pytest.mark.asyncio
async def test_relock_is_rejected_and_leaves_no_trace() -> None:
    """LOCKED 是终态：不是「再锁一次也无害」，而是改规格必须新修订 + 新规格。"""
    locked = _spec(entity_id="spec-1", status=ShotSpecStatus.LOCKED, locked_dimensions="camera")
    specs = _Specs(seed=[locked])
    service, _, _, _, outbox = _service(specs=specs)

    with pytest.raises(DomainError) as exc:
        await _lock(service, entity_id="spec-1")

    assert exc.value.error_code.code == "STATE_TRANSITION_DENIED"
    assert "已锁定" in exc.value.detail
    assert specs.saved == [] and outbox.transitions == []


@pytest.mark.asyncio
async def test_lock_writes_transition_fact_and_event_in_one_pass() -> None:
    """四要素齐备时：状态、版本、事实（含 authority_ref）、事件同一次推进产生。"""
    specs = _Specs(seed=[_spec(entity_id="spec-1")])
    service, _, _, _, outbox = _service(specs=specs)

    spec = await _lock(service, entity_id="spec-1")

    assert spec.status is ShotSpecStatus.LOCKED
    assert spec.locked_dimensions == "camera,subject", "集合语义：排序去重"
    assert spec.entity_version == 2
    assert specs.saved == [("spec-1", 1)], "乐观锁：以推进前版本为条件"

    assert len(outbox.transitions) == 1
    fact, event = outbox.transitions[0]
    assert fact.resource_type == "ShotSpec"
    assert fact.resource_id == "spec-1"
    assert fact.from_status == "DRAFT" and fact.to_status == "LOCKED"
    assert fact.owner_action == "SHOTSPEC.LOCK", "准入凭据与事实同源"
    assert fact.actor_subject == "director"
    assert fact.authority_ref == "approval://board/2026-11/7", "锁定依据必须可追溯"
    assert fact.entity_version == 2

    assert event.event_type == "SHOTSPEC_LOCKED"
    assert event.payload["locked_dimensions"] == "camera,subject"
    assert event.payload["lock_basis_ref"] == "approval://board/2026-11/7"
    assert event.payload["from_status"] == "DRAFT"
    assert event.payload["to_status"] == "LOCKED"
    assert event.payload["locked_by"] == "director"
    assert event.payload["tenant_id"] == str(_TENANT)
