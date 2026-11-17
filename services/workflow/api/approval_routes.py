"""通用审批引擎 API（薄层；动作与响应结构均来自冻结契约）。

请求体刻意不接受：决策人、投票时间、票据状态、结论。它们分别来自已验签
Principal、服务端时钟、状态机与投票计算——能由调用方填的字段就会成为伪造审计的入口。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.identity.authorization import ResourceRef
from packages.identity.enforcement import authorize
from packages.shared.contract_models import contract_model
from packages.shared.outbox_writer import SqlAlchemyOutboxWriter
from packages.shared.unit_of_work import UnitOfWork
from services.identity_access.deps import (
    Principal,
    get_workflow_uow,
    require_action,
)
from services.workflow.domain.enums import VoteDecision
from services.workflow.domain.ports import ResourceOwnershipPort
from services.workflow.repository.sqlalchemy_approval_repo import (
    SqlAlchemyApprovalRepository,
)
from services.workflow.service.approval_service import ApprovalService

router = APIRouter(prefix="/approval-tickets", tags=["workflow"])

_ApprovalTicketResponse = contract_model("ApprovalTicket")


class StartApprovalRequest(BaseModel):
    target_action_key: str = Field(..., min_length=1, max_length=96)
    resource_type: str = Field(..., min_length=1, max_length=96)
    resource_id: str = Field(..., min_length=1, max_length=96)
    project_id: str = Field(..., min_length=1, max_length=64)
    ruleset_version: int = Field(..., ge=1)
    entity_id: str | None = Field(default=None, max_length=64)


class SubmitVoteRequest(BaseModel):
    group_name: str = Field(..., min_length=1, max_length=64)
    decision: VoteDecision
    reason: str = Field(..., min_length=1)
    entity_id: str | None = Field(default=None, max_length=64)


def _get_ownership_port(request: Request) -> ResourceOwnershipPort:
    """取组合根挂载的跨域归属解析端口（apps/api lifespan 装配）。

    fail-closed：端口缺失说明进程装配不完整，拒绝发起而不是盲发票据。
    """
    port: ResourceOwnershipPort | None = getattr(
        request.app.state, "resource_ownership_port", None
    )
    if port is None:
        raise DomainError(
            ErrorRegistry.INTERNAL_ERROR,
            detail="归属解析端口未装配（app.state.resource_ownership_port 缺失），拒绝发起审批",
        )
    return port


def _build_service(
    session: AsyncSession, ownership: ResourceOwnershipPort
) -> ApprovalService:
    return ApprovalService(
        SqlAlchemyApprovalRepository(session),
        SqlAlchemyOutboxWriter(session),
        ownership,
    )


def _get_service(
    uow: UnitOfWork = Depends(get_workflow_uow),
    ownership: ResourceOwnershipPort = Depends(_get_ownership_port),
) -> ApprovalService:
    return _build_service(uow.session, ownership)


@router.post("", status_code=201, response_model=_ApprovalTicketResponse)
async def start_approval(
    body: StartApprovalRequest,
    principal: Principal = Depends(require_action("WORKFLOW.START_APPROVAL")),
    uow: UnitOfWork = Depends(get_workflow_uow),
    service: ApprovalService = Depends(_get_service),
) -> dict[str, Any]:
    """按目标动作的审批绑定发起票据；发起人不具备投票权。"""
    authorize(
        principal,
        "WORKFLOW.START_APPROVAL",
        resource=ResourceRef(
            resource_type="ApprovalTicket",
            organization_id=principal.organization_id,
            project_id=body.project_id,
        ),
    )
    ticket = await service.start(
        target_action_key=body.target_action_key,
        resource_type=body.resource_type,
        resource_id=body.resource_id,
        project_id=body.project_id,
        ruleset_version=body.ruleset_version,
        principal=principal,
        entity_id=body.entity_id,
    )
    await uow.commit()
    return ticket.to_dict()


@router.post(
    "/{ticket_id}/votes", status_code=201, response_model=_ApprovalTicketResponse
)
async def submit_vote(
    ticket_id: str,
    body: SubmitVoteRequest,
    principal: Principal = Depends(require_action("WORKFLOW.SUBMIT_VOTE")),
    uow: UnitOfWork = Depends(get_workflow_uow),
    service: ApprovalService = Depends(_get_service),
) -> dict[str, Any]:
    """投一票（同一分组同一主体只允许一票；结论成立发完成事件）。"""
    await service.submit_vote(
        ticket_id=ticket_id,
        group_name=body.group_name,
        decision=body.decision,
        reason=body.reason,
        principal=principal,
        entity_id=body.entity_id,
    )
    # 响应票据必须在 commit **之前**读：SET LOCAL 注入的 RLS 上下文随事务
    # 结束而清除，commit 后再查会因看不到任何行而误报 404（诊断实测过）。
    # 同一事务内读取，能看到 submit_vote 里乐观锁推进后的最新状态。
    ticket = await service.get_ticket(ticket_id)
    await uow.commit()
    return ticket.to_dict()
