"""ProductionRoute 聚合根与状态机单测（纯领域层，无 IO）。"""

from __future__ import annotations

import uuid

import pytest

from packages.errors.error_codes import DomainError
from services.generation_execution.domain.enums import ProductionRouteStatus
from services.generation_execution.domain.production_route import ProductionRoute
from services.generation_execution.domain.state_machine import (
    assert_transition_production_route,
    can_transition_production_route,
)


def _route(**overrides: object) -> ProductionRoute:
    """构造一条合法的 UNVERIFIED 路线（测试基线对象）。"""
    defaults: dict[str, object] = {
        "entity_id": f"route_{uuid.uuid4().hex[:8]}",
        "route_type": "blender_scripted",
        "supported_control_signals": ["camera_path"],
        "required_appearance_refs": [],
        "qc_dimensions": ["geometry_validity"],
        "capability_refs": [],
        "created_by": "ops_seed",
        "tenant_id": uuid.uuid4(),
    }
    defaults.update(overrides)
    return ProductionRoute(**defaults)  # type: ignore[arg-type]


class TestProductionRouteAggregate:
    def test_construct_unverified_minimal(self) -> None:
        route = _route()
        assert route.status is ProductionRouteStatus.UNVERIFIED
        assert route.owner_module == "generation-execution"
        assert route.ENTITY_TYPE == "ProductionRoute"

    def test_verified_requires_capability_evidence(self) -> None:
        """16.2：VERIFIED 必须有证据背书，无 capability_refs 拒绝。"""
        with pytest.raises(DomainError) as exc_info:
            _route(status=ProductionRouteStatus.VERIFIED)
        assert exc_info.value.error_code.code == "VALIDATION_ERROR"

    def test_verified_with_evidence_ok(self) -> None:
        route = _route(
            status=ProductionRouteStatus.VERIFIED,
            capability_refs=["evidence_pkg_001"],
        )
        assert route.status is ProductionRouteStatus.VERIFIED

    def test_unverified_route_type_must_be_registered(self) -> None:
        with pytest.raises(DomainError) as exc_info:
            _route(route_type="not_a_route_type")
        assert exc_info.value.error_code.code == "VALIDATION_ERROR"

    def test_qc_dimensions_required(self) -> None:
        """QC 覆盖维度是选路与 Gate 依据，空集拒绝。"""
        with pytest.raises(DomainError) as exc_info:
            _route(qc_dimensions=[])
        assert exc_info.value.error_code.code == "VALIDATION_ERROR"

    def test_to_dict_maps_route_id(self) -> None:
        route = _route()
        data = route.to_dict()
        assert data["route_id"] == route.entity_id
        assert data["entity_type"] == "ProductionRoute"
        assert data["status"] == "UNVERIFIED"
        # 契约字段集对齐：A6 冻结定义里没有 project_id。
        assert "project_id" not in data


class TestProductionRouteStateMachine:
    def test_only_edge_is_unverified_to_verified(self) -> None:
        assert can_transition_production_route(
            ProductionRouteStatus.UNVERIFIED, ProductionRouteStatus.VERIFIED
        )
        # VERIFIED 无出边（终态不可原地复活）。
        assert not can_transition_production_route(
            ProductionRouteStatus.VERIFIED, ProductionRouteStatus.UNVERIFIED
        )

    def test_assert_transition_denied_raises(self) -> None:
        with pytest.raises(DomainError) as exc_info:
            assert_transition_production_route(
                ProductionRouteStatus.VERIFIED, ProductionRouteStatus.UNVERIFIED
            )
        assert exc_info.value.error_code.code == "STATE_TRANSITION_DENIED"
