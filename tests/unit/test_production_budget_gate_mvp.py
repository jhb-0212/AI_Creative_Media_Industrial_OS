"""R16-D3 生产链与预算 MVP 的单元测试。

覆盖六个 action 背后的领域对象与应用服务：
- ProductionRoute：选择即 VERIFIED；治理边 VERIFIED→REVOKED 归 SELECT；
- GenerationTask：REQUEST 同步推进 SUCCEEDED / FAILED；
- ShotBudgetRequest：DRAFT→APPROVED/REJECTED；审批人职责分离；
- BudgetLedger：追加即不可变、NO_LIFECYCLE 哨兵、幂等键；
- GateDecision：追加即不可变、决策值受控。
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from packages.errors.error_codes import DomainError
from packages.shared.in_memory import InMemoryOutboxWriter, InMemoryRepository
from services.budget_quota.domain.budget_ledger import BudgetLedger
from services.budget_quota.service.budget_service import BudgetService
from services.production_planning.domain.production_route import ProductionRoute
from services.production_planning.service.generation_service import GenerationService
from services.production_planning.service.production_route_service import (
    ProductionRouteService,
)
from services.project_planning.domain.enums import (
    GenerationTaskStatus,
    NoLifecycleStatus,
    ProductionRouteStatus,
    ShotBudgetRequestStatus,
)
from services.project_planning.domain.state_machine import (
    PRODUCTION_ROUTE_SELECT_ACTION,
)
from services.qc_governance.domain.gate_decision import GateDecision
from services.qc_governance.service.gate_service import GateService

_TENANT = uuid4()


def _route(*, status: ProductionRouteStatus = ProductionRouteStatus.VERIFIED) -> ProductionRoute:
    return ProductionRoute(
        entity_id="route-1",
        created_by="td",
        tenant_id=_TENANT,
        route_id="route-1",
        project_id="proj-1",
        shot_id="shot-1",
        task_ref="task-1",
        route_type="IMAGE_TO_VIDEO",
        capability_ref="cap-1",
        basis_ref="basis-1",
        status=status,
    )


def test_production_route_to_dict_matches_schema_keys() -> None:
    data = _route().to_dict()
    assert set(data) == {
        "entity_id",
        "entity_type",
        "schema_version",
        "entity_version",
        "status",
        "owner_module",
        "created_at",
        "created_by",
        "updated_at",
        "audit_ref",
        "route_id",
        "project_id",
        "shot_id",
        "task_ref",
        "route_type",
        "capability_ref",
        "basis_ref",
    }
    assert data["status"] == "VERIFIED"
    assert data["owner_module"] == "production-planning"


def test_production_route_revoke_edge_owned_by_select() -> None:
    route = _route()
    route.transition_to(
        ProductionRouteStatus.REVOKED, "audit-1", PRODUCTION_ROUTE_SELECT_ACTION
    )
    assert route.status is ProductionRouteStatus.REVOKED
    assert route.entity_version == 2


def test_production_route_revoke_wrong_action_rejected() -> None:
    route = _route()
    with pytest.raises(DomainError):
        route.transition_to(ProductionRouteStatus.REVOKED, "audit-1", "WRONG.ACTION")


async def test_production_route_service_emits_event() -> None:
    repo = InMemoryRepository()
    outbox = InMemoryOutboxWriter()
    service = ProductionRouteService(repo, outbox)
    route = await service.select_route(
        project_id="proj-1",
        shot_id="shot-1",
        task_ref="task-1",
        route_type="IMAGE_TO_VIDEO",
        capability_ref="cap-1",
        basis_ref="basis-1",
        created_by="td",
        tenant_id=_TENANT,
    )
    assert route.status is ProductionRouteStatus.VERIFIED
    assert await repo.get_by_id(route.entity_id) is route
    assert outbox.events[0]["event_type"] == "PRODUCTION_ROUTE_SELECTED"
    assert outbox.events[0]["payload"]["route_type"] == "IMAGE_TO_VIDEO"
    assert outbox.events[0]["payload"]["tenant_id"] == str(_TENANT)


async def test_generation_request_advances_to_succeeded() -> None:
    repo = InMemoryRepository()
    outbox = InMemoryOutboxWriter()
    service = GenerationService(repo, outbox)
    task = await service.request_generation(
        project_id="proj-1",
        shot_id="shot-1",
        shot_revision_id="rev-1",
        production_route_ref="route-1",
        model_adapter_ref="adapter-1",
        constraint_snapshot_id="snap-1",
        budget_ref="budget-1",
        prompt_ref="prompt-1",
        result="SUCCEEDED",
        created_by="director",
        tenant_id=_TENANT,
    )
    assert task.status is GenerationTaskStatus.SUCCEEDED
    assert task.entity_version == 2
    assert outbox.events[0]["event_type"] == "GENERATION_REQUESTED"
    assert outbox.events[0]["payload"]["from_status"] == "REQUESTED"
    assert outbox.events[0]["payload"]["to_status"] == "SUCCEEDED"


async def test_generation_request_advances_to_failed() -> None:
    repo = InMemoryRepository()
    outbox = InMemoryOutboxWriter()
    service = GenerationService(repo, outbox)
    task = await service.request_generation(
        project_id="proj-1",
        shot_id="shot-1",
        shot_revision_id="rev-1",
        production_route_ref="route-1",
        model_adapter_ref="adapter-1",
        constraint_snapshot_id="snap-1",
        budget_ref="budget-1",
        prompt_ref="prompt-1",
        result="FAILED",
        created_by="director",
        tenant_id=_TENANT,
    )
    assert task.status is GenerationTaskStatus.FAILED


async def test_budget_request_then_approve_appends_ledger() -> None:
    repo = InMemoryRepository()
    ledger_repo = InMemoryRepository()
    outbox = InMemoryOutboxWriter()
    service = BudgetService(repo, ledger_repo, outbox)

    request = await service.request_budget(
        project_id="proj-1",
        shot_id="shot-1",
        account_type="PRODUCTION",
        purpose="render",
        cost_category="render",
        amount_limit=1000,
        requested_by="agent",
        tenant_id=_TENANT,
    )
    assert request.status is ShotBudgetRequestStatus.DRAFT

    approved = await service.approve_budget(
        entity_id=request.entity_id,
        decision="APPROVED",
        approver="producer",
        decision_reason="",
        tenant_id=_TENANT,
    )
    assert approved.status is ShotBudgetRequestStatus.APPROVED
    # 账本原子追加一条 RESERVE 流水
    ledger_rows = list(ledger_repo.snapshot().values())
    assert len(ledger_rows) == 1
    ledger = ledger_rows[0]
    assert ledger.amount == 1000
    assert ledger.idempotency_key == f"shot-budget:{request.request_id}"
    assert ledger.status is NoLifecycleStatus.NO_LIFECYCLE
    # 审批事件
    event = outbox.events[-1]
    assert event["event_type"] == "SHOT_BUDGET_APPROVED"
    assert event["payload"]["decision"] == "APPROVED"


async def test_budget_self_approval_rejected() -> None:
    repo = InMemoryRepository()
    ledger_repo = InMemoryRepository()
    outbox = InMemoryOutboxWriter()
    service = BudgetService(repo, ledger_repo, outbox)
    request = await service.request_budget(
        project_id="proj-1",
        shot_id="shot-1",
        account_type="PRODUCTION",
        purpose="render",
        cost_category="render",
        amount_limit=1000,
        requested_by="same-person",
        tenant_id=_TENANT,
    )
    with pytest.raises(DomainError):
        await service.approve_budget(
            entity_id=request.entity_id,
            decision="APPROVED",
            approver="same-person",
            decision_reason="",
            tenant_id=_TENANT,
        )


async def test_budget_reject_requires_reason() -> None:
    repo = InMemoryRepository()
    ledger_repo = InMemoryRepository()
    outbox = InMemoryOutboxWriter()
    service = BudgetService(repo, ledger_repo, outbox)
    request = await service.request_budget(
        project_id="proj-1",
        shot_id="shot-1",
        account_type="PRODUCTION",
        purpose="render",
        cost_category="render",
        amount_limit=1000,
        requested_by="agent",
        tenant_id=_TENANT,
    )
    with pytest.raises(DomainError):
        await service.approve_budget(
            entity_id=request.entity_id,
            decision="REJECTED",
            approver="producer",
            decision_reason="",
            tenant_id=_TENANT,
        )


def test_budget_ledger_requires_idempotency_key() -> None:
    with pytest.raises(DomainError):
        BudgetLedger(
            entity_id="ledger-1",
            created_by="producer",
            tenant_id=_TENANT,
            transaction_id="tx-1",
            account_type="PRODUCTION",
            amount=1000,
            currency="CNY",
            transaction_kind="RESERVE",
            idempotency_key="",
            approval_ref="req-1",
            source="req-1",
        )


async def test_gate_decision_approve() -> None:
    repo = InMemoryRepository()
    outbox = InMemoryOutboxWriter()
    service = GateService(repo, outbox)
    record = await service.approve_gate(
        gate_key="GATE_1",
        project_id="proj-1",
        decision="APPROVED",
        decided_by="gate-owner",
        tenant_id=_TENANT,
        reason_code="PASS",
    )
    assert record.decision == "APPROVED"
    assert record.status is NoLifecycleStatus.NO_LIFECYCLE
    assert outbox.events[0]["event_type"] == "PRODUCTION_GATE_APPROVED"
    assert outbox.events[0]["payload"]["gate_key"] == "GATE_1"


async def test_production_route_revoke_via_service() -> None:
    """撤销走同一动作的治理边：VERIFIED → REVOKED 且事件带撤销依据。"""
    repo = InMemoryRepository()
    outbox = InMemoryOutboxWriter()
    service = ProductionRouteService(repo, outbox)
    route = await service.select_route(
        project_id="proj-1",
        shot_id="shot-1",
        task_ref="task-1",
        route_type="IMAGE_TO_VIDEO",
        capability_ref="cap-1",
        basis_ref="basis-1",
        created_by="td",
        tenant_id=_TENANT,
    )
    revoked = await service.revoke_route(
        entity_id=route.entity_id,
        revoked_by="producer",
        revocation_ref="rights-expired-2026",
        tenant_id=_TENANT,
    )
    assert revoked.status is ProductionRouteStatus.REVOKED
    assert revoked.entity_version == 2
    event = outbox.events[-1]
    assert event["event_type"] == "PRODUCTION_ROUTE_REVOKED"
    assert event["payload"]["revocation_ref"] == "rights-expired-2026"
    assert event["payload"]["revoked_by"] == "producer"
    assert event["payload"]["from_status"] == "VERIFIED"
    assert event["payload"]["to_status"] == "REVOKED"


async def test_production_route_revoke_requires_basis() -> None:
    """没有依据的撤销无法被审计复核，必须在推进前拒绝。"""
    repo = InMemoryRepository()
    outbox = InMemoryOutboxWriter()
    service = ProductionRouteService(repo, outbox)
    route = await service.select_route(
        project_id="proj-1",
        shot_id="shot-1",
        task_ref="task-1",
        route_type="IMAGE_TO_VIDEO",
        capability_ref="cap-1",
        basis_ref="basis-1",
        created_by="td",
        tenant_id=_TENANT,
    )
    with pytest.raises(DomainError):
        await service.revoke_route(
            entity_id=route.entity_id,
            revoked_by="producer",
            revocation_ref="   ",
            tenant_id=_TENANT,
        )
    # 拒绝后聚合必须回到推进前：库里仍是 VERIFIED。
    assert route.status is ProductionRouteStatus.VERIFIED


def test_gate_decision_rejects_unknown_decision() -> None:
    with pytest.raises(DomainError):
        GateDecision(
            entity_id="gd-1",
            created_by="gate-owner",
            tenant_id=_TENANT,
            decision="MAYBE",
            gate_key="GATE_1",
            project_id="proj-1",
            decided_by="gate-owner",
            constraint_snapshot_id="snap",
            gate_instance_id="inst",
            reason_code="X",
        )
