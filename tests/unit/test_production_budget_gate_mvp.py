"""R16-D3 生产链与预算 MVP 的单元测试。

覆盖六个 action 背后的领域对象与应用服务：
- ProductionRoute：选择即 VERIFIED；治理边 VERIFIED→REVOKED 归 SELECT；
- GenerationTask：REQUEST 同步推进 SUCCEEDED / FAILED；
- ShotBudgetRequest：DRAFT→APPROVED/REJECTED；审批人职责分离；
- BudgetLedger：追加即不可变、NO_LIFECYCLE 哨兵、幂等键；
- GateDecision：追加即不可变、决策值受控。
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from packages.errors.error_codes import DomainError
from packages.shared.in_memory import InMemoryOutboxWriter, InMemoryRepository
from packages.shared.quality_profile import UNIFIED_HIGH_QUALITY
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
        quality_profile_version=UNIFIED_HIGH_QUALITY,
        status=status,
    )


def test_production_route_to_dict_matches_schema_keys() -> None:
    """字段集从契约派生，不手写清单。

    C4 给 ProductionRoute 增加 quality_profile_version 后，手写集合的旧断言会
    以「多出一个字段」的形式失败——那会让修复方向变成「把新字段补进清单」，
    而下一轮契约演进又重演一次。契约 schema 才是唯一事实源：to_dict 的键集
    必须精确等于它的 required 集合（本对象全部属性均必填）。
    """
    schema_path = (
        Path(__file__).resolve().parents[2]
        / "contracts"
        / "schemas"
        / "object_schemas.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    route_schema = schema["definitions"]["ProductionRoute"]
    expected = set(route_schema["required"])

    data = _route().to_dict()
    assert set(data) == expected, (
        f"to_dict 字段集与契约 required 不一致："
        f"缺 {sorted(expected - set(data))} / 多 {sorted(set(data) - expected)}"
    )
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


async def test_generation_request_stays_requested() -> None:
    """创建只登记请求，不推进终态：执行结果只能由持租约的执行器提交。

    曾经的实现接受 ``result`` 且默认 ``SUCCEEDED``——调用方不传就算成功，
    既无真实产物也无执行证据。这条测试钉住「请求方不能声明执行结果」。
    """
    repo = InMemoryRepository()
    outbox = InMemoryOutboxWriter()
    service = GenerationService(repo, outbox)
    await repo.add(_route())  # C4：先有归属一致的 VERIFIED 路线
    task = await service.request_generation(
        project_id="proj-1",
        shot_id="shot-1",
        shot_revision_id="rev-1",
        production_route_ref="route-1",
        model_adapter_ref="adapter-1",
        constraint_snapshot_id="snap-1",
        budget_ref="budget-1",
        prompt_ref="prompt-1",
        created_by="director",
        tenant_id=_TENANT,
    )
    assert task.status is GenerationTaskStatus.REQUESTED
    assert task.entity_version == 1
    assert task.asset_version_id == ""
    # C4：任务创建即从路线冻结权威质量策略，不再等到 T1 才发现为空。
    assert task.quality_profile_version == UNIFIED_HIGH_QUALITY


async def test_generation_request_event_is_not_a_transition_fact() -> None:
    """创建事件不得带上 ``from_status`` / ``to_status``。

    创建没有前驱状态，把它写成状态迁移会让事实表撒谎
    （``ck_status_transition_advances`` 要求 from ≠ to）。审计要看到
    「有人请求了生成」，事实表却不该凭空出现一条迁移。
    ``advance_status`` 恒补这两个键，``record`` 恒不补——用它区分两者。
    """
    repo = InMemoryRepository()
    outbox = InMemoryOutboxWriter()
    service = GenerationService(repo, outbox)
    await repo.add(_route())  # C4：先有归属一致的 VERIFIED 路线
    task = await service.request_generation(
        project_id="proj-1",
        shot_id="shot-1",
        shot_revision_id="rev-1",
        production_route_ref="route-1",
        model_adapter_ref="adapter-1",
        constraint_snapshot_id="snap-1",
        budget_ref="budget-1",
        prompt_ref="prompt-1",
        created_by="director",
        tenant_id=_TENANT,
    )
    assert len(outbox.events) == 1
    event = outbox.events[0]
    assert event["event_type"] == "GENERATION_REQUESTED"
    assert event["aggregate_id"] == task.entity_id
    assert "from_status" not in event["payload"]
    assert "to_status" not in event["payload"]
    assert event["payload"]["created_by"] == "director"


async def test_selection_routes_bind_authoritative_quality_profile() -> None:
    """C4：路线一创建就绑定契约单值，不靠请求方填写。"""
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
    assert route.quality_profile_version == UNIFIED_HIGH_QUALITY


async def test_task_freezes_route_quality_profile_at_creation() -> None:
    """C4：任务从路线冻结 QP，且路线撤销后不能再作为新任务的依据。"""
    repo = InMemoryRepository()
    outbox = InMemoryOutboxWriter()
    route = await ProductionRouteService(repo, outbox).select_route(
        project_id="proj-1",
        shot_id="shot-1",
        task_ref="task-1",
        route_type="IMAGE_TO_VIDEO",
        capability_ref="cap-1",
        basis_ref="basis-1",
        created_by="td",
        tenant_id=_TENANT,
        route_id="route-1",
    )
    service = GenerationService(repo, outbox)
    task = await service.request_generation(
        project_id="proj-1",
        shot_id="shot-1",
        shot_revision_id="rev-1",
        production_route_ref=route.route_id,
        model_adapter_ref="adapter-1",
        constraint_snapshot_id="snap-1",
        budget_ref="budget-1",
        prompt_ref="prompt-1",
        created_by="director",
        tenant_id=_TENANT,
    )
    assert task.quality_profile_version == UNIFIED_HIGH_QUALITY

    # 撤销路线后，同一引用不可再创建新任务（不能借已失效的 QP 依据受理）。
    # 内存仓储按引用存储，原地 transition_to 即反映为库里的状态。
    route.transition_to(
        ProductionRouteStatus.REVOKED,
        "audit-revoke",
        PRODUCTION_ROUTE_SELECT_ACTION,
    )
    with pytest.raises(DomainError):
        await service.request_generation(
            project_id="proj-1",
            shot_id="shot-1",
            shot_revision_id="rev-2",
            production_route_ref=route.route_id,
            model_adapter_ref="adapter-1",
            constraint_snapshot_id="snap-1",
            budget_ref="budget-1",
            prompt_ref="prompt-1",
            created_by="director",
            tenant_id=_TENANT,
        )


async def test_task_rejects_route_from_another_project() -> None:
    """C4：跨项目借用路线必须在创建时拒绝（route_id 是业务短号，非全局身份键）。"""
    repo = InMemoryRepository()
    outbox = InMemoryOutboxWriter()
    await ProductionRouteService(repo, outbox).select_route(
        project_id="proj-OTHER",
        shot_id="shot-1",
        task_ref="task-1",
        route_type="IMAGE_TO_VIDEO",
        capability_ref="cap-1",
        basis_ref="basis-1",
        created_by="td",
        tenant_id=_TENANT,
        route_id="route-1",
    )
    service = GenerationService(repo, outbox)
    with pytest.raises(DomainError):
        await service.request_generation(
            project_id="proj-1",
            shot_id="shot-1",
            shot_revision_id="rev-1",
            production_route_ref="route-1",
            model_adapter_ref="adapter-1",
            constraint_snapshot_id="snap-1",
            budget_ref="budget-1",
            prompt_ref="prompt-1",
            created_by="director",
            tenant_id=_TENANT,
        )


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
