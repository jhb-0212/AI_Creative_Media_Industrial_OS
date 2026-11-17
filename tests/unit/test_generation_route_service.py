"""RouteService 用例单元测试（内存 fake 仓储，无数据库依赖）。

覆盖（PRODUCTION.ROUTE.SELECT / ACT-ROUTE-NEG-001）：
- select_route：VERIFIED 路线选中并发出 PRODUCTION_ROUTE_SELECTED 审计事件；
- UNVERIFIED 路线拒绝（未验证能力不得进入生产路由）；
- 幽灵路线 404（NOT_FOUND）；
- 必填上下文缺失拒绝（route_id/project_id/shot_id）。
"""

from __future__ import annotations

import uuid
from uuid import uuid4

import pytest

from packages.errors.error_codes import DomainError
from packages.identity.principal import Principal, SubjectType
from packages.shared.outbox_writer import OutboxWriter
from services.generation_execution.domain.enums import ProductionRouteStatus
from services.generation_execution.domain.production_route import ProductionRoute
from services.generation_execution.repository.production_route_repo import (
    ProductionRouteRepository,
)
from services.generation_execution.service.route_service import RouteService

_TENANT = uuid.UUID("00000000-0000-0000-0000-000000000000")


def _principal(
    subject_id: str = "producer_001",
    subject_type: SubjectType = SubjectType.HUMAN,
) -> Principal:
    return Principal(
        subject_id=subject_id,
        tenant_id=_TENANT,
        organization_id=_TENANT,
        subject_type=subject_type,
        roles=frozenset({"制片"}),
        project_ids=frozenset({"*"}),
    )


class _RecordingOutboxWriter(OutboxWriter):
    def __init__(self) -> None:
        self.events: list[dict] = []

    def record(self, *, aggregate_type, aggregate_id, tenant_id, event_type, payload):  # type: ignore[override]
        self.events.append({"event_type": event_type, "payload": payload})
        return uuid4()


class _FakeRouteRepository(ProductionRouteRepository):
    def __init__(self) -> None:
        self._store: dict[str, ProductionRoute] = {}

    def seed(self, route: ProductionRoute) -> None:
        self._store[route.entity_id] = route

    async def add(self, route: ProductionRoute) -> None:
        self._store[route.entity_id] = route

    async def get_by_id(self, entity_id: str) -> ProductionRoute | None:
        return self._store.get(entity_id)


def _service(
    store: _FakeRouteRepository | None = None,
) -> tuple[RouteService, _FakeRouteRepository, _RecordingOutboxWriter]:
    repo = store or _FakeRouteRepository()
    outbox = _RecordingOutboxWriter()
    return RouteService(repo, outbox), repo, outbox


def _seed_route(
    repo: _FakeRouteRepository,
    *,
    route_id: str = "route_001",
    status: ProductionRouteStatus = ProductionRouteStatus.VERIFIED,
) -> ProductionRoute:
    route = ProductionRoute(
        entity_id=route_id,
        route_type="blender_scripted",
        supported_control_signals=["camera_path"],
        required_appearance_refs=[],
        qc_dimensions=["geometry_validity"],
        capability_refs=["evidence_pkg_001"] if status is ProductionRouteStatus.VERIFIED else [],
        status=status,
        created_by="ops_seed",
        tenant_id=_TENANT,
    )
    repo.seed(route)
    return route


class TestSelectRoute:
    @pytest.mark.asyncio
    async def test_verified_route_selected_and_event_emitted(self) -> None:
        service, repo, outbox = _service()
        route = _seed_route(repo, status=ProductionRouteStatus.VERIFIED)

        selected = await service.select_route(
            route_id=route.entity_id,
            project_id="proj_001",
            shot_id="SHOT_001",
            principal=_principal(),
        )

        assert selected.entity_id == route.entity_id
        # 选中不改变路线状态：选择是"使用事实"，路线状态是"能力事实"。
        assert selected.status is ProductionRouteStatus.VERIFIED
        assert len(outbox.events) == 1
        event = outbox.events[0]
        assert event["event_type"] == "PRODUCTION_ROUTE_SELECTED"
        payload = event["payload"]
        for key in (
            "route_id",
            "route_type",
            "project_id",
            "shot_id",
            "selected_by",
            "tenant_id",
            "status",
        ):
            assert key in payload, f"审计事件缺契约登记字段: {key}"
        assert payload["route_id"] == route.entity_id
        assert payload["selected_by"] == "producer_001"

    @pytest.mark.asyncio
    async def test_unverified_route_rejected(self) -> None:
        """ACT-ROUTE-NEG-001：UNVERIFIED 路线拒绝。"""
        service, repo, outbox = _service()
        route = _seed_route(repo, status=ProductionRouteStatus.UNVERIFIED)

        with pytest.raises(DomainError) as exc_info:
            await service.select_route(
                route_id=route.entity_id,
                project_id="proj_001",
                shot_id="SHOT_001",
                principal=_principal(),
            )
        assert exc_info.value.error_code.code == "VALIDATION_ERROR"
        assert outbox.events == [], "拒绝路径不得发出审计事件"

    @pytest.mark.asyncio
    async def test_ghost_route_not_found(self) -> None:
        service, _repo, outbox = _service()
        with pytest.raises(DomainError) as exc_info:
            await service.select_route(
                route_id="route_missing",
                project_id="proj_001",
                shot_id="SHOT_001",
                principal=_principal(),
            )
        assert exc_info.value.error_code.code == "NOT_FOUND"
        assert outbox.events == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("field", ["route_id", "project_id", "shot_id"])
    async def test_missing_context_rejected(self, field: str) -> None:
        service, _repo, _outbox = _service()
        kwargs: dict[str, str] = {
            "route_id": "route_001",
            "project_id": "proj_001",
            "shot_id": "SHOT_001",
        }
        kwargs[field] = "  "
        with pytest.raises(DomainError) as exc_info:
            await service.select_route(principal=_principal(), **kwargs)
        assert exc_info.value.error_code.code == "VALIDATION_ERROR"
