"""GenerationService 受理用例单测（fake 仓储/端口，纯领域交互）。"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.identity.principal import Principal, SubjectType
from packages.shared.outbox_writer import OutboxWriter
from services.generation_execution.domain.enums import (
    GenerationTaskStatus,
    ProductionRouteStatus,
)
from services.generation_execution.domain.generation_task import GenerationTask
from services.generation_execution.domain.ports import RefOwnership
from services.generation_execution.domain.production_route import ProductionRoute
from services.generation_execution.domain.state_machine import (
    GENERATION_TASK_ALLOWED_TRANSITIONS,
)
from services.generation_execution.repository.generation_task_repo import (
    GenerationTaskRepository,
)
from services.generation_execution.repository.production_route_repo import (
    ProductionRouteRepository,
)
from services.generation_execution.service.generation_service import GenerationService

_TENANT = UUID("00000000-0000-0000-0000-000000000000")

#: fake 归属表的默认登记：引用对象都在 proj_001。
_DEFAULT_OWNERSHIP: dict[str, str] = {
    "ShotSpec:spec_001": "proj_001",
    "ShotVisualPlan:vplan_001": "proj_001",
}


def _principal(subject_id: str = "producer_001") -> Principal:
    return Principal(
        subject_id=subject_id,
        tenant_id=_TENANT,
        organization_id=_TENANT,
        subject_type=SubjectType.HUMAN,
        roles=frozenset({"制片"}),
        project_ids=frozenset({"*"}),
    )


class _RecordingOutboxWriter(OutboxWriter):
    def __init__(self) -> None:
        self.events: list[dict] = []

    def record(self, *, aggregate_type, aggregate_id, tenant_id, event_type, payload):  # type: ignore[override]
        self.events.append({"event_type": event_type, "payload": payload})
        return uuid4()


class _FakeTaskRepository(GenerationTaskRepository):
    def __init__(self) -> None:
        self._store: dict[str, GenerationTask] = {}

    async def add(self, task: GenerationTask) -> None:
        self._store[task.entity_id] = task

    async def get_by_id(self, entity_id: str) -> GenerationTask | None:
        return self._store.get(entity_id)

    async def save(
        self, task: GenerationTask, *, expected_version: int
    ) -> GenerationTask:
        stored = self._store[task.entity_id]
        if stored.entity_version != expected_version:
            raise DomainError(
                ErrorRegistry.CONCURRENT_MODIFICATION, detail="版本冲突"
            )
        self._store[task.entity_id] = task
        return task

    async def list_pending(self, limit: int) -> list[GenerationTask]:
        return [
            t for t in self._store.values() if t.status is GenerationTaskStatus.PENDING
        ][:limit]


class _FakeRouteRepository(ProductionRouteRepository):
    def __init__(self) -> None:
        self._store: dict[str, ProductionRoute] = {}

    def seed(self, route: ProductionRoute) -> None:
        self._store[route.entity_id] = route

    async def add(self, route: ProductionRoute) -> None:
        self._store[route.entity_id] = route

    async def get_by_id(self, entity_id: str) -> ProductionRoute | None:
        return self._store.get(entity_id)


class _FakeRefOwnershipPort:
    """登记式 fake：resource_id → project_id 映射；未登记类型 fail-closed。"""

    def __init__(self, ownership: dict[str, str]) -> None:
        self._ownership = ownership

    def can_resolve(self, resource_type: str) -> bool:
        return resource_type in {"ShotSpec", "ShotVisualPlan", "ConstraintPack"}

    async def resolve(
        self,
        *,
        tenant_id: object,
        organization_id: object,
        resource_type: str,
        resource_id: str,
    ) -> RefOwnership | None:
        project = self._ownership.get(f"{resource_type}:{resource_id}")
        return RefOwnership(project_id=project) if project else None


class _Harness:
    """测试装配：service + 全部 fake，可按用例覆写归属表。"""

    def __init__(self, ownership: dict[str, str] | None = None) -> None:
        self.tasks = _FakeTaskRepository()
        self.routes = _FakeRouteRepository()
        self.outbox = _RecordingOutboxWriter()
        self.port = _FakeRefOwnershipPort(
            ownership if ownership is not None else dict(_DEFAULT_OWNERSHIP)
        )
        self.service = GenerationService(
            self.tasks, self.routes, self.port, self.outbox
        )

    def seed_verified_route(self, route_id: str = "route_001") -> None:
        self.routes.seed(
            ProductionRoute(
                entity_id=route_id,
                route_type="blender_scripted",
                supported_control_signals=["camera_path"],
                required_appearance_refs=[],
                qc_dimensions=["geometry_validity"],
                capability_refs=["evidence_pkg_001"],
                status=ProductionRouteStatus.VERIFIED,
                created_by="ops_seed",
                tenant_id=_TENANT,
            )
        )

    def seed_unverified_route(self, route_id: str) -> None:
        self.routes.seed(
            ProductionRoute(
                entity_id=route_id,
                route_type="blender_scripted",
                supported_control_signals=[],
                required_appearance_refs=[],
                qc_dimensions=["geometry_validity"],
                capability_refs=[],
                status=ProductionRouteStatus.UNVERIFIED,
                created_by="ops_seed",
                tenant_id=_TENANT,
            )
        )


def _request_args(**overrides: object) -> dict[str, object]:
    defaults: dict[str, object] = {
        "project_id": "proj_001",
        "shot_id": "SHOT_001",
        "route_id": "route_001",
        "shot_spec_ref": "spec_001",
        "visual_plan_ref": "vplan_001",
        "constraint_snapshot_ref": "NOT_APPLICABLE",
        "budget_reservation_ref": "budget_res_001",
        "quota_reservation_ref": "quota_res_001",
        "dependency_refs": [],
        "principal": _principal(),
    }
    defaults.update(overrides)
    return defaults


def _seed_failed_task(
    tasks: _FakeTaskRepository,
    *,
    entity_id: str = "gen_failed_001",
    project_id: str = "proj_001",
    attempt: int = 1,
    status: GenerationTaskStatus = GenerationTaskStatus.FAILED,
) -> GenerationTask:
    task = GenerationTask(
        entity_id=entity_id,
        project_id=project_id,
        shot_id="SHOT_001",
        route_id="route_001",
        shot_spec_ref="spec_001",
        visual_plan_ref="vplan_001",
        constraint_snapshot_ref="NOT_APPLICABLE",
        budget_reservation_ref="b",
        quota_reservation_ref="q",
        attempt=attempt,
        created_by="producer_001",
        tenant_id=_TENANT,
        status=status,
    )
    tasks._store[entity_id] = task  # 直接落库（fake 存储语义）
    return task


class TestRequestGenerationAdmission:
    @pytest.mark.asyncio
    async def test_admitted_task_is_pending_and_event_emitted(self) -> None:
        h = _Harness()
        h.seed_verified_route()

        task = await h.service.request_generation(**_request_args())

        assert task.status is GenerationTaskStatus.PENDING
        assert task.attempt == 1
        assert task.retry_of is None
        stored = await h.tasks.get_by_id(task.entity_id)
        assert stored is task
        assert len(h.outbox.events) == 1
        event = h.outbox.events[0]
        assert event["event_type"] == "GENERATION_REQUESTED"
        assert event["payload"]["generation_task_id"] == task.entity_id
        assert event["payload"]["project_id"] == "proj_001"

    @pytest.mark.asyncio
    async def test_unverified_route_rejected(self) -> None:
        """ACT-ROUTE-NEG-001 同源语义：UNVERIFIED 路线不得进入生成。"""
        h = _Harness()
        h.seed_unverified_route("route_unverified")
        with pytest.raises(DomainError) as exc_info:
            await h.service.request_generation(
                **_request_args(route_id="route_unverified")
            )
        assert exc_info.value.error_code.code == "VALIDATION_ERROR"

    @pytest.mark.asyncio
    async def test_ghost_route_not_found(self) -> None:
        h = _Harness()
        with pytest.raises(DomainError) as exc_info:
            await h.service.request_generation(**_request_args(route_id="route_missing"))
        assert exc_info.value.error_code.code == "NOT_FOUND"

    @pytest.mark.asyncio
    async def test_ghost_spec_ref_not_found(self) -> None:
        """幽灵引用 404：引用对象不存在。"""
        h = _Harness()
        h.seed_verified_route()
        with pytest.raises(DomainError) as exc_info:
            await h.service.request_generation(
                **_request_args(shot_spec_ref="spec_ghost")
            )
        assert exc_info.value.error_code.code == "NOT_FOUND"

    @pytest.mark.asyncio
    async def test_cross_project_ref_rejected(self) -> None:
        """ACT-GEN-NEG-001：跨项目引用 = 权限越界拒绝（422）。"""
        h = _Harness(ownership={"ShotSpec:spec_001": "proj_other"})
        h.seed_verified_route()
        with pytest.raises(DomainError) as exc_info:
            await h.service.request_generation(**_request_args())
        assert exc_info.value.error_code.code == "VALIDATION_ERROR"
        assert "ACT-GEN-NEG-001" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_budget_ref_empty_rejected(self) -> None:
        """预算/配额引用结构性准入：空引用拒绝（数值校验是 ADR-R32 债务）。"""
        h = _Harness()
        h.seed_verified_route()
        with pytest.raises(DomainError) as exc_info:
            await h.service.request_generation(
                **_request_args(budget_reservation_ref=" ")
            )
        assert exc_info.value.error_code.code == "VALIDATION_ERROR"
        assert "ACT-GEN-NEG-001" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_constraint_pack_ref_existence_enforced(self) -> None:
        """非哨兵约束快照引用必须可解析（fake 未登记 → 404）。"""
        h = _Harness()
        h.seed_verified_route()
        with pytest.raises(DomainError) as exc_info:
            await h.service.request_generation(
                **_request_args(constraint_snapshot_ref="pack_001")
            )
        assert exc_info.value.error_code.code == "NOT_FOUND"

    @pytest.mark.asyncio
    async def test_constraint_pack_not_applicable_sentinel_ok(self) -> None:
        """31.3：无锁定约束镜头标记 NOT_APPLICABLE，不做归属解析。"""
        h = _Harness()
        h.seed_verified_route()
        task = await h.service.request_generation(**_request_args())
        assert task.constraint_snapshot_ref == "NOT_APPLICABLE"


class TestRetryChain:
    @pytest.mark.asyncio
    async def test_retry_of_failed_task_increments_attempt(self) -> None:
        """重试以新任务承载：引用 FAILED 终态任务，attempt+1。"""
        h = _Harness()
        h.seed_verified_route()
        _seed_failed_task(h.tasks)

        task = await h.service.request_generation(
            **_request_args(retry_of="gen_failed_001")
        )

        assert task.attempt == 2
        assert task.retry_of == "gen_failed_001"
        assert len(h.outbox.events) == 1

    @pytest.mark.asyncio
    async def test_retry_of_non_failed_rejected(self) -> None:
        """只有 FAILED 可被重试承载：SUCCEEDED 终态拒绝。"""
        h = _Harness()
        h.seed_verified_route()
        _seed_failed_task(h.tasks, entity_id="gen_ok_001", status=GenerationTaskStatus.SUCCEEDED)
        with pytest.raises(DomainError) as exc_info:
            await h.service.request_generation(**_request_args(retry_of="gen_ok_001"))
        assert exc_info.value.error_code.code == "VALIDATION_ERROR"

    @pytest.mark.asyncio
    async def test_retry_of_ghost_rejected(self) -> None:
        h = _Harness()
        h.seed_verified_route()
        with pytest.raises(DomainError) as exc_info:
            await h.service.request_generation(**_request_args(retry_of="gen_missing"))
        assert exc_info.value.error_code.code == "NOT_FOUND"

    @pytest.mark.asyncio
    async def test_retry_of_cross_project_rejected(self) -> None:
        h = _Harness()
        h.seed_verified_route()
        _seed_failed_task(h.tasks, entity_id="gen_other_proj", project_id="proj_other")
        with pytest.raises(DomainError):
            await h.service.request_generation(**_request_args(retry_of="gen_other_proj"))


class TestTransitionTableConsistency:
    def test_registry_transitions_match_state_machine(self) -> None:
        """领域转移表覆盖全部枚举状态（防漂移）。"""
        assert set(GENERATION_TASK_ALLOWED_TRANSITIONS.keys()) == set(
            GenerationTaskStatus
        )
