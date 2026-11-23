"""生成任务 API 路由（薄层，R16-D3 / Phase 2 §1）。

路由只做三件事：校验入参、取 UoW、调服务；所有状态与租约判定在领域层。
租约相关端点共用一个 action_key ``GENERATION.PROCESS``——它们表达同一个语义
「推进一个已有任务」，拆成多个 action 会让同一条边出现多个准入凭据。

租约持有者取自 **Principal**，不取自请求体
------------------------------------------
``lease_owner`` 是「谁在跑这个任务」的事实，而它的唯一作用是让
``require_lease_owner`` 拒绝非持有者续租/终结。只要这个字符串来自请求体，
那条判定就等于「谁知道这个名字，谁就是持有者」——身份是自称的，租约退化成
一个公开的字符串比较。所以三个租约端点的请求模型都没有 ``worker_id``，
并由 ``extra="forbid"`` 让旧调用方带上它时直接 422，而不是被静默忽略后
误以为自己指定了身份。
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from packages.shared.unit_of_work import UnitOfWork
from services.identity_access.deps import (
    Principal,
    authorize_on_parent,
    get_production_planning_tenant_uow,
    require_role,
)
from services.production_planning.repository.sqlalchemy_repos import (
    SqlAlchemyGenerationTaskRepository,
)
from services.production_planning.service.generation_service import GenerationService
from services.project_planning.repository.sqlalchemy_outbox_writer import (
    SqlAlchemyOutboxWriter,
)

router = APIRouter(prefix="/generation-tasks", tags=["generation-task"])

# 租约时长的上下界：太短会让正常执行也在心跳间隙被回收，太长会让崩溃任务的
# 恢复窗口拖到无人值守的深夜。默认 5 分钟，上限 1 小时。
_LEASE_MIN_SECONDS = 30
_LEASE_MAX_SECONDS = 3600


def _service(session: AsyncSession) -> GenerationService:
    return GenerationService(
        SqlAlchemyGenerationTaskRepository(session),
        SqlAlchemyOutboxWriter(session),
    )


def _get_service(
    uow: UnitOfWork = Depends(get_production_planning_tenant_uow),
) -> GenerationService:
    return _service(uow.session)


class RequestGenerationRequest(BaseModel):
    """创建生成任务。

    请求方只能提出生成请求，不能声明执行结果：本模型没有 ``result`` 字段，
    且 ``extra="forbid"`` 让旧调用方传 ``result`` 时立刻收到 422，而不是被
    静默忽略后误以为自己「提交了成功」。任务创建后一律停在 REQUESTED，
    由持租约的执行器经 ``/{entity_id}/finish`` 提交执行事实。
    """

    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(..., min_length=1, max_length=64)
    shot_id: str = Field(..., min_length=1, max_length=64)
    shot_revision_id: str = Field(..., min_length=1, max_length=64)
    production_route_ref: str = Field(..., min_length=1, max_length=64)
    model_adapter_ref: str = Field(..., min_length=1, max_length=128)
    constraint_snapshot_id: str = Field(..., min_length=1, max_length=128)
    budget_ref: str = Field(..., min_length=1, max_length=128)
    prompt_ref: str = Field(..., min_length=1, max_length=200)
    generation_task_id: str = Field("", max_length=64)


class LeaseClaimRequest(BaseModel):
    """领取租约（REQUESTED → RUNNING）。

    **没有** ``worker_id``：租约持有者取自已认证的 Principal（见模块文档）。
    ``extra="forbid"`` 让旧调用方传 ``worker_id`` 时立刻 422，而不是被静默忽略后
    误以为自己指定了身份。
    """

    model_config = ConfigDict(extra="forbid")

    lease_seconds: int = Field(300, ge=_LEASE_MIN_SECONDS, le=_LEASE_MAX_SECONDS)


class LeaseRenewRequest(BaseModel):
    """续租（不迁移状态）。持有者同样取自 Principal。"""

    model_config = ConfigDict(extra="forbid")

    lease_seconds: int = Field(300, ge=_LEASE_MIN_SECONDS, le=_LEASE_MAX_SECONDS)


class FinishRequest(BaseModel):
    """终结失败任务（RUNNING → FAILED）。

    成功终态由资产登记回执驱动的 T3 推进（ADR-R42 C1/C3）：请求体里出现
    ``asset_version_id`` / ``quality_profile_version`` 就是让 Worker 自报登记
    结果，因此这里以 ``extra="forbid"`` 明确拒绝，而不是静默忽略。

    同样没有 ``worker_id``：持有者身份由 Principal 决定，不由请求体自称。
    """

    model_config = ConfigDict(extra="forbid")

    result: Literal["FAILED"]
    evidence_ref: str = Field("", max_length=200)


@router.post("", status_code=201)
async def request_generation(
    body: RequestGenerationRequest,
    principal: Principal = Depends(require_role("GENERATION.REQUEST")),
    uow: UnitOfWork = Depends(get_production_planning_tenant_uow),
    service: GenerationService = Depends(_get_service),
) -> dict[str, Any]:
    """创建生成任务：一律停在 REQUESTED，由持租约的执行器提交执行结果。"""
    await authorize_on_parent(
        "GENERATION.REQUEST",
        principal,
        uow.session,
        parent_type="Project",
        parent_id=body.project_id,
    )
    task = await service.request_generation(
        project_id=body.project_id,
        shot_id=body.shot_id,
        shot_revision_id=body.shot_revision_id,
        production_route_ref=body.production_route_ref,
        model_adapter_ref=body.model_adapter_ref,
        constraint_snapshot_id=body.constraint_snapshot_id,
        budget_ref=body.budget_ref,
        prompt_ref=body.prompt_ref,
        created_by=principal.subject_id,
        tenant_id=principal.tenant_id,
        generation_task_id=body.generation_task_id,
    )
    await uow.commit()
    return task.to_dict()


@router.post("/{entity_id}/lease")
async def claim_lease(
    entity_id: str,
    body: LeaseClaimRequest,
    principal: Principal = Depends(require_role("GENERATION.PROCESS")),
    uow: UnitOfWork = Depends(get_production_planning_tenant_uow),
    service: GenerationService = Depends(_get_service),
) -> dict[str, Any]:
    """领取租约：只有无人持有且租约未过期时才会从 REQUESTED 推进到 RUNNING。"""
    task = await service.claim(
        task_id=entity_id,
        worker_id=principal.subject_id,
        lease_seconds=body.lease_seconds,
    )
    await uow.commit()
    return task.to_dict()


@router.post("/{entity_id}/lease/renew")
async def renew_lease(
    entity_id: str,
    body: LeaseRenewRequest,
    principal: Principal = Depends(require_role("GENERATION.PROCESS")),
    uow: UnitOfWork = Depends(get_production_planning_tenant_uow),
    service: GenerationService = Depends(_get_service),
) -> dict[str, Any]:
    """心跳续租：非持有者会被拒绝。"""
    task = await service.renew(
        task_id=entity_id,
        worker_id=principal.subject_id,
        lease_seconds=body.lease_seconds,
    )
    await uow.commit()
    return task.to_dict()


@router.post("/{entity_id}/lease/release")
async def release_expired_lease(
    entity_id: str,
    principal: Principal = Depends(require_role("GENERATION.PROCESS")),
    uow: UnitOfWork = Depends(get_production_planning_tenant_uow),
    service: GenerationService = Depends(_get_service),
) -> dict[str, Any]:
    """回收到期租约：RUNNING → REQUESTED，任务重新可领取。"""
    task = await service.release_expired(task_id=entity_id)
    await uow.commit()
    return task.to_dict()


@router.post("/{entity_id}/finish")
async def finish_task(
    entity_id: str,
    body: FinishRequest,
    principal: Principal = Depends(require_role("GENERATION.PROCESS")),
    uow: UnitOfWork = Depends(get_production_planning_tenant_uow),
    service: GenerationService = Depends(_get_service),
) -> dict[str, Any]:
    """登记失败终态：成功必须由资产回执经 T3 推进，不能从请求体自报。"""
    task = await service.finish(
        task_id=entity_id,
        worker_id=principal.subject_id,
        result=body.result,
        evidence_ref=body.evidence_ref,
    )
    await uow.commit()
    return task.to_dict()


__all__ = ["router"]
