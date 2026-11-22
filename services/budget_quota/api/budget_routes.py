"""单镜头预算申请与审批 API 路由（薄层，R16-D3）。"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from packages.shared.unit_of_work import UnitOfWork
from services.budget_quota.repository.sqlalchemy_repos import (
    SqlAlchemyBudgetLedgerRepository,
    SqlAlchemyShotBudgetRequestRepository,
)
from services.budget_quota.service.budget_service import BudgetService
from services.identity_access.deps import (
    Principal,
    authorize_on_parent,
    get_budget_quota_tenant_uow,
    require_resource_role,
    require_role,
)
from services.project_planning.repository.sqlalchemy_outbox_writer import (
    SqlAlchemyOutboxWriter,
)

router = APIRouter(prefix="/shot-budget-requests", tags=["shot-budget"])
ledger_router = APIRouter(prefix="/budget-ledger", tags=["budget-ledger"])


def _service(session: AsyncSession) -> BudgetService:
    return BudgetService(
        SqlAlchemyShotBudgetRequestRepository(session),
        SqlAlchemyBudgetLedgerRepository(session),
        SqlAlchemyOutboxWriter(session),
    )


def _get_service(uow: UnitOfWork = Depends(get_budget_quota_tenant_uow)) -> BudgetService:
    return _service(uow.session)


class RequestShotBudgetRequest(BaseModel):
    """提交单镜头预算申请（创建即 DRAFT）。"""

    project_id: str = Field(..., min_length=1, max_length=64)
    shot_id: str = Field(..., min_length=1, max_length=64)
    account_type: str = Field(..., min_length=1, max_length=32)
    purpose: str = Field(..., min_length=1, max_length=128)
    cost_category: str = Field(..., min_length=1, max_length=64)
    amount_limit: int = Field(..., ge=0)
    request_id: str = Field("", max_length=64)


class ApproveShotBudgetRequest(BaseModel):
    """审批单镜头预算申请（DRAFT → APPROVED / REJECTED）。"""

    decision: Literal["APPROVED", "REJECTED"]
    approver: str = Field(..., min_length=1, max_length=64)
    decision_reason: str = Field("", max_length=200)


@router.post("", status_code=201)
async def request_shot_budget(
    body: RequestShotBudgetRequest,
    principal: Principal = Depends(require_role("BUDGET.SHOT.REQUEST")),
    uow: UnitOfWork = Depends(get_budget_quota_tenant_uow),
    service: BudgetService = Depends(_get_service),
) -> dict[str, Any]:
    """提交单镜头预算申请。归属判定对象是父项目（申请尚不存在）。"""
    await authorize_on_parent(
        "BUDGET.SHOT.REQUEST",
        principal,
        uow.session,
        parent_type="Project",
        parent_id=body.project_id,
    )
    request = await service.request_budget(
        project_id=body.project_id,
        shot_id=body.shot_id,
        account_type=body.account_type,
        purpose=body.purpose,
        cost_category=body.cost_category,
        amount_limit=body.amount_limit,
        requested_by=principal.subject_id,
        tenant_id=principal.tenant_id,
        request_id=body.request_id,
    )
    await uow.commit()
    return request.to_dict()


@router.post("/{entity_id}/approve")
async def approve_shot_budget(
    entity_id: str,
    body: ApproveShotBudgetRequest,
    principal: Principal = Depends(
        # 审批是资源级动作：判定对象是这条申请本身（归属经 projects 一跳解析），
        # 而不是请求体里自称的项目 id —— 后者可以由调用方任意填写。
        require_resource_role(
            "BUDGET.SHOT.APPROVE",
            resource_type="ShotBudgetRequest",
            param_name="entity_id",
            uow_dep=get_budget_quota_tenant_uow,
        )
    ),
    uow: UnitOfWork = Depends(get_budget_quota_tenant_uow),
    service: BudgetService = Depends(_get_service),
) -> dict[str, Any]:
    """审批预算申请；APPROVED 时原子追加预算账本流水（同一事务）。"""
    request = await service.approve_budget(
        entity_id=entity_id,
        decision=body.decision,
        approver=body.approver,
        decision_reason=body.decision_reason,
        tenant_id=principal.tenant_id,
    )
    await uow.commit()
    return request.to_dict()


class SettleBudgetRequest(BaseModel):
    """释放 / 冲销一笔预留言本。"""

    amount: int = Field(..., gt=0)
    reason: str = Field("", max_length=200)


@ledger_router.post("/{entity_id}/release", status_code=201)
async def release_budget(
    entity_id: str,
    body: SettleBudgetRequest,
    principal: Principal = Depends(require_role("BUDGET.SHOT.RELEASE")),
    uow: UnitOfWork = Depends(get_budget_quota_tenant_uow),
    service: BudgetService = Depends(_get_service),
) -> dict[str, Any]:
    """释放已预留未消耗的额度（追加一条 RELEASE 流水）。"""
    ledger = await service.release_budget(
        reservation_ledger_id=entity_id,
        amount=body.amount,
        actor=principal.subject_id,
        tenant_id=principal.tenant_id,
        reason=body.reason,
    )
    await uow.commit()
    return ledger.to_dict()


@ledger_router.post("/{entity_id}/refund", status_code=201)
async def refund_budget(
    entity_id: str,
    body: SettleBudgetRequest,
    principal: Principal = Depends(require_role("BUDGET.SHOT.REFUND")),
    uow: UnitOfWork = Depends(get_budget_quota_tenant_uow),
    service: BudgetService = Depends(_get_service),
) -> dict[str, Any]:
    """对已结算额度做冲销（追加一条 REFUND 流水，不修改原流水）。"""
    ledger = await service.refund_budget(
        reservation_ledger_id=entity_id,
        amount=body.amount,
        actor=principal.subject_id,
        tenant_id=principal.tenant_id,
        reason=body.reason,
    )
    await uow.commit()
    return ledger.to_dict()


__all__ = ["ledger_router", "router"]
