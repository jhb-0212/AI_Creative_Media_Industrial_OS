"""QC 待检任务 API 路由（薄层，Phase 3）。

路由只做三件事：校验入参、取 UoW、调服务；状态与租约判定在领域层。
租约相关端点共用一个 action_key ``QC.TASK.PROCESS``：它们表达同一语义
「推进一个已有 QC 任务」，拆成多个 action 会让同一条边出现多个准入凭据。

租约持有者取自 **Principal**，不取自请求体
------------------------------------------
``lease_owner`` 的唯一作用，是让 ``require_lease_owner`` 拒绝非持有者续租/判定。
只要这个字符串来自请求体，那条判定就等于「谁知道这个名字，谁就是持有者」——
身份是自称的，租约退化成一个公开的字符串比较，任何人都能接管别人的检测任务
并写出 PASSED 判定（判定本身是 QC 存在的意义）。所以三个租约端点没有
``worker_id``，并由 ``extra="forbid"`` 让旧调用方带上它时直接 422。
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from packages.shared.evidence_ports import ReportLookup
from packages.shared.unit_of_work import UnitOfWork
from services.identity_access.deps import (
    Principal,
    authorize_on_parent,
    get_asset_tenant_uow,
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

# ``app.state`` 上报告核验器工厂的键。与 ``apps/api/wiring.py`` 同名常量保持一致；
# 这里写字面量而不是反向 import，是因为 services 不得依赖 apps（R1）。
_LOOKUP_FACTORY_STATE_KEY = "qc_report_lookup_factory"


def _service(session: AsyncSession) -> QCTaskService:
    return QCTaskService(
        SqlAlchemyQCTaskRepository(session),
        SqlAlchemyOutboxWriter(session),
    )


def _get_service(
    uow: UnitOfWork = Depends(get_qc_governance_tenant_uow),
) -> QCTaskService:
    return _service(uow.session)


def _get_lookup_factory(request: Request) -> Any:
    """取组合根装配的核验器工厂；未装配时返回 ``None``（由服务层 fail-closed）。

    为什么不在这里直接构造：报告属资产域，QC 域 import 资产域仓储会违反
    分层规则 R2。端口由本域声明、适配由资产域提供、装配由 ``apps/`` 完成——
    见 ``apps/api/wiring.py`` 的说明。未装配时不在此报 500，而是交给服务层
    抛 ``INTERNAL_ERROR``：拒绝理由应写在**判定发生的地方**，而不是散落在路由。
    """
    return getattr(request.app.state, _LOOKUP_FACTORY_STATE_KEY, None)


class EnqueueQCTaskRequest(BaseModel):
    """生成终态入队。"""

    project_id: str = Field(..., min_length=1, max_length=64)
    asset_version_id: str = Field(..., min_length=1, max_length=64)
    generation_task_id: str = Field(..., min_length=1, max_length=64)
    quality_profile_version: str = Field(..., min_length=1, max_length=64)
    shot_id: str = Field("", max_length=64)
    qc_task_id: str = Field("", max_length=64)


class LeaseClaimRequest(BaseModel):
    """领取 QC 任务：持有者取自 Principal（不由请求体自称）。"""

    model_config = ConfigDict(extra="forbid")

    lease_seconds: int = Field(300, ge=_LEASE_MIN_SECONDS, le=_LEASE_MAX_SECONDS)


class LeaseRenewRequest(BaseModel):
    """心跳续租：持有者取自 Principal。"""

    model_config = ConfigDict(extra="forbid")

    lease_seconds: int = Field(300, ge=_LEASE_MIN_SECONDS, le=_LEASE_MAX_SECONDS)


class FinishRequest(BaseModel):
    """QC 判定（RUNNING → PASSED / REJECTED）：报告不可为空。"""

    model_config = ConfigDict(extra="forbid")

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
        worker_id=principal.subject_id,
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
        worker_id=principal.subject_id,
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
    asset_uow: UnitOfWork = Depends(get_asset_tenant_uow),
    lookup_factory: Any = Depends(_get_lookup_factory),
) -> dict[str, Any]:
    """判定：只有租约持有者可推进终态，且必须有**可核验**的 QC 报告。

    「有报告 ID」不等于「有证据」：报告必须在资产域真实存在，且属于本任务
    引用的资产版本与同一租户。核验跨域读一次资产域，值得——否则任何持有
    ``QC.TASK.PROCESS`` 的调用方提交一串随便编的 ID 就能推成 PASSED。
    """
    report_lookup: ReportLookup | None = (
        lookup_factory(asset_uow.session) if lookup_factory is not None else None
    )
    service = QCTaskService(
        SqlAlchemyQCTaskRepository(uow.session),
        SqlAlchemyOutboxWriter(uow.session),
        report_lookup=report_lookup,
    )
    task = await service.finish(
        task_id=entity_id,
        worker_id=principal.subject_id,
        result=body.result,
        qc_report_id=body.qc_report_id,
        evidence_refs=body.evidence_refs,
    )
    await uow.commit()
    return task.to_dict()


__all__ = ["router"]
