"""Gate 决策 API 路由（薄层，R16-D3）。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from packages.shared.unit_of_work import UnitOfWork
from services.identity_access.deps import (
    Principal,
    authorize_on_parent,
    get_qc_governance_tenant_uow,
    require_role,
)
from services.project_planning.repository.sqlalchemy_outbox_writer import (
    SqlAlchemyOutboxWriter,
)
from services.qc_governance.repository.sqlalchemy_repo import (
    SqlAlchemyGateDecisionRepository,
)
from services.qc_governance.service.gate_service import GateService

router = APIRouter(prefix="/gate-decisions", tags=["gate-decision"])


def _service(session: AsyncSession) -> GateService:
    return GateService(
        SqlAlchemyGateDecisionRepository(session),
        SqlAlchemyOutboxWriter(session),
    )


def _get_service(uow: UnitOfWork = Depends(get_qc_governance_tenant_uow)) -> GateService:
    return _service(uow.session)


class ApproveGateRequest(BaseModel):
    """记录一次 Gate 决策（APPROVED / REJECTED）。"""

    gate_key: str = Field(..., min_length=1, max_length=64)
    project_id: str = Field(..., min_length=1, max_length=64)
    decision: str = Field(..., pattern="^(APPROVED|REJECTED)$")
    gate_instance_id: str = Field("", max_length=64)
    constraint_snapshot_id: str = Field("", max_length=128)
    entry_criteria: str = Field("", max_length=500)
    reason_code: str = Field("", max_length=64)
    comment: str = Field("", max_length=500)
    required_artifacts: str = Field("", max_length=500)
    evidence_refs: list[str] = Field(default_factory=list)
    reviewers: list[str] = Field(default_factory=list)
    ruleset_version: int = Field(1, ge=1)
    scope: dict[str, Any] = Field(default_factory=dict)


@router.post("", status_code=201)
async def approve_gate(
    body: ApproveGateRequest,
    principal: Principal = Depends(require_role("PRODUCTION.GATE.APPROVE")),
    uow: UnitOfWork = Depends(get_qc_governance_tenant_uow),
    service: GateService = Depends(_get_service),
) -> dict[str, Any]:
    """记录一次 Gate 决策。

    决策与它的审批事件同处一个事务：任一侧失败都整体回滚，不留下「有事件、
    无记录」的形态（那正是此前内存仓储的失效模式）。
    """
    await authorize_on_parent(
        "PRODUCTION.GATE.APPROVE",
        principal,
        uow.session,
        parent_type="Project",
        parent_id=body.project_id,
    )
    record = await service.approve_gate(
        gate_key=body.gate_key,
        project_id=body.project_id,
        decision=body.decision,
        decided_by=principal.subject_id,
        tenant_id=principal.tenant_id,
        gate_instance_id=body.gate_instance_id,
        constraint_snapshot_id=body.constraint_snapshot_id,
        entry_criteria=body.entry_criteria,
        reason_code=body.reason_code,
        comment=body.comment,
        required_artifacts=body.required_artifacts,
        evidence_refs=body.evidence_refs,
        reviewers=body.reviewers,
        ruleset_version=body.ruleset_version,
        scope=body.scope,
    )
    await uow.commit()
    return record.to_dict()


__all__ = ["router"]
