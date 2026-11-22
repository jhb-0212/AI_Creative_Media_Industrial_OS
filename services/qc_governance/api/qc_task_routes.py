"""QC 待检任务 API 路由（薄层，Phase 3）。

路由只做三件事：校验入参、取 UoW、调服务；状态与租约判定在领域层。
租约相关端点共用一个 action_key ``QC.TASK.PROCESS``：它们表达同一语义
「推进一个已有 QC 任务」，拆成多个 action 会让同一条边出现多个准入凭据。
"""

from __future__ import annotations

from typing import Any, Literal

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
from services.qc_governance.repository.qc_task_repo import SqlAlchemyQCTaskRepository
from services.qc_governance.service.qc_task_service import QCTaskService

router = APIRouter(prefix="/qc-tasks", tags=["qc-task"])

_LEASE_MIN_SECONDS = 30
_LEASE_MAX_SECONDS = 3600


def _service(session: AsyncSession) -> QCTaskService:
    return QCTaskService(
        SqlAlchemyQCTaskRepository(session),
        SqlAlchemyOutboxWriter(session),
    )


def _get_service(
    uow: UnitOfWork = Depends(get_qc_governance_tenant_uow),
) -> QCTaskService:
    return _service(uow.session)


class EnqueueQCTaskRequest(BaseModel):
    """生成终态入队。"""

    project_id: str = Field(..., min_length=1, max_length=64)
    asset_version_id: str = Field(..., min_length=1, max_length=64)
    generation_task_id: str = Field(..., min_length=1, max_length=64)
    quality_profile_version: str = Field(..., min_length=1, max_length=64)
    shot_id: str = Field("", max_length=64)
    qc_task_id: str = Field("", max_length=64)


class LeaseClaimRequest(BaseModel):
    worker_id: str = Field(..., min_length=1, max_length=128)
    lease_seconds: int = Field(300, ge=_LEASE_MIN_SECONDS, le=_LEASE_MAX_SECONDS)


class LeaseRenewRequest(BaseModel):
    worker_id: str = Field(..., min_length=1, max_length=128)
    lease_seconds: int = Field(300, ge=_LEASE_MIN_SECONDS, le=_LEASE_MAX_SECONDS)


class FinishRequest(BaseModel):
    """QC 判定（RUNNING → PASSED / REJECTED）：报告不可为空。"""

    worker_id: str = Field(..., min_length=1, max_length=128)
    result: Literal["PASSED", "REJECTED"]
    qc_report_id: str = Field(..., min_length=1, max_length=64)
    evidence_refs: list[str] = Field(default_factory=list)


@router.post("", status_code=201)
async def enqueue_qc_task(
    body: EnqueueQCTaskRequest,
    principal: Principal = Depends(require_role("QC.TASK.ENQUEUE")),
    uow: UnitOfWork = Depends(get_qc_governance_tenant_uow),
    service: QCTaskService = Depends(_get_service),
) -> dict[str, Any]:
    """生成终态入队：同一生成任务重复入队会被拒绝（业务冲突）。"""
    await authorize_on_parent(
        "QC.TASK.ENQUEUE",
        principal,
        uow.session,
        parent_type="Project",
        parent_id=body.project_id,
    )
    task = await service.enqueue(
        project_id=body.project_id,
        asset_version_id=body.asset_version_id,
        generation_task_id=body.generation_task_id,
        quality_profile_version=body.quality_profile_version,
        created_by=principal.subject_id,
        tenant_id=principal.tenant_id,
        shot_id=body.shot_id,
        qc_task_id=body.qc_task_id,
    )
    await uow.commit()
    return task.to_dict()


@router.post("/{entity_id}/lease")
async def claim_qc_lease(
    entity_id: str,
    body: LeaseClaimRequest,
    principal: Principal = Depends(require_role("QC.TASK.PROCESS")),
    uow: UnitOfWork = Depends(get_qc_governance_tenant_uow),
    service: QCTaskService = Depends(_get_service),
) -> dict[str, Any]:
    """领取 QC 任务：QUEUED → RUNNING，并落下租约。"""
    task = await service.claim(
        task_id=entity_id,
        worker_id=body.worker_id,
        lease_seconds=body.lease_seconds,
    )
    await uow.commit()
    return task.to_dict()


@router.post("/{entity_id}/lease/renew")
async def renew_qc_lease(
    entity_id: str,
    body: LeaseRenewRequest,
    principal: Principal = Depends(require_role("QC.TASK.PROCESS")),
    uow: UnitOfWork = Depends(get_qc_governance_tenant_uow),
    service: QCTaskService = Depends(_get_service),
) -> dict[str, Any]:
    """心跳续租：非持有者被拒绝。"""
    task = await service.renew(
        task_id=entity_id,
        worker_id=body.worker_id,
        lease_seconds=body.lease_seconds,
    )
    await uow.commit()
    return task.to_dict()


@router.post("/{entity_id}/lease/release")
async def release_qc_lease(
    entity_id: str,
    principal: Principal = Depends(require_role("QC.TASK.PROCESS")),
    uow: UnitOfWork = Depends(get_qc_governance_tenant_uow),
    service: QCTaskService = Depends(_get_service),
) -> dict[str, Any]:
    """回收到期租约：RUNNING → QUEUED，任务重新可领取。"""
    task = await service.release_expired(task_id=entity_id)
    await uow.commit()
    return task.to_dict()


@router.post("/{entity_id}/finish")
async def finish_qc_task(
    entity_id: str,
    body: FinishRequest,
    principal: Principal = Depends(require_role("QC.TASK.PROCESS")),
    uow: UnitOfWork = Depends(get_qc_governance_tenant_uow),
    service: QCTaskService = Depends(_get_service),
) -> dict[str, Any]:
    """判定：只有租约持有者可推进终态，且必须挂 QC 报告。"""
    task = await service.finish(
        task_id=entity_id,
        worker_id=body.worker_id,
        result=body.result,
        qc_report_id=body.qc_report_id,
        evidence_refs=body.evidence_refs,
    )
    await uow.commit()
    return task.to_dict()


__all__ = ["router"]
