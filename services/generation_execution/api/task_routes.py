"""GenerationTask API 路由（薄层，只做协议适配与 DTO 转换）。

契约对齐（动作矩阵 / openapi.draft /actions/generation.request）：
- GENERATION.REQUEST：受理生成任务（MEDIUM，导演/制片/授权范围内 Agent）；
  准入由"预算、配额、依赖与约束快照通过"（ACT-GEN-NEG-001）；
- 请求/响应走统一信封与错误信封；业务逻辑下沉到 GenerationService；
- 引用归属解析端口由组合根经 app.state 注入（ADR-R28 机制复用，
  读经过所有者域自己的数据库身份）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.identity.authorization import ResourceRef
from packages.identity.enforcement import authorize
from packages.shared.outbox_writer import SqlAlchemyOutboxWriter
from packages.shared.unit_of_work import UnitOfWork
from services.generation_execution.domain.ports import RefOwnershipPort
from services.generation_execution.repository.generation_task_sqlalchemy_repo import (
    SqlAlchemyGenerationTaskRepository,
)
from services.generation_execution.repository.production_route_sqlalchemy_repo import (
    SqlAlchemyProductionRouteRepository,
)
from services.generation_execution.service.generation_service import GenerationService
from services.identity_access.deps import (
    Principal,
    get_generation_uow,
    require_action,
)

router = APIRouter(prefix="/actions", tags=["generation-execution"])


class GenerationRequest(BaseModel):
    """生成受理请求体（身份不在请求体，由 Principal 提供）。"""

    project_id: str = Field(..., min_length=1, max_length=64)
    shot_id: str = Field(..., min_length=1, max_length=64)
    route_id: str = Field(..., min_length=1, max_length=64)
    shot_spec_ref: str = Field(..., min_length=1, max_length=64)
    visual_plan_ref: str = Field(..., min_length=1, max_length=64)
    constraint_snapshot_ref: str = Field(..., min_length=1, max_length=64)
    budget_reservation_ref: str = Field(..., min_length=1, max_length=64)
    quota_reservation_ref: str = Field(..., min_length=1, max_length=64)
    dependency_refs: list[str] = Field(default_factory=list)
    retry_of: str | None = Field(default=None, max_length=64)
    #: 本次产出的权利凭据引用（可空；缺失时产出可登记但不可批准，ADR-R33）。
    rights_manifest_ref: str | None = Field(default=None, max_length=64)


class GenerationRequestAccepted(BaseModel):
    """受理回执（对齐 openapi.draft ActionAccepted）。"""

    status: str = "ACCEPTED"
    audit_event: str = "GENERATION_REQUESTED"
    generation_task_id: str
    project_id: str
    shot_id: str
    route_id: str
    attempt: int
    task_status: str


def _get_ref_ownership_port(request: Request) -> RefOwnershipPort:
    """从组合根取引用归属解析端口（lifespan 装配，缺失即装配缺陷）。"""
    port = getattr(request.app.state, "generation_ref_ownership_port", None)
    if port is None:
        raise DomainError(
            ErrorRegistry.INTERNAL_ERROR,
            detail="引用归属解析端口未装配（组合根 lifespan 缺失）",
        )
    typed_port: RefOwnershipPort = port
    return typed_port


def _get_service(
    uow: UnitOfWork = Depends(get_generation_uow),
    ref_port: RefOwnershipPort = Depends(_get_ref_ownership_port),
) -> GenerationService:
    """写用例服务装配（提交点由路由显式控制）。"""
    return GenerationService(
        SqlAlchemyGenerationTaskRepository(uow.session),
        SqlAlchemyProductionRouteRepository(uow.session),
        ref_port,
        SqlAlchemyOutboxWriter(uow.session),
    )


@router.post("/generation.request", response_model=GenerationRequestAccepted)
async def request_generation(
    body: GenerationRequest,
    principal: Principal = Depends(require_action("GENERATION.REQUEST")),
    uow: UnitOfWork = Depends(get_generation_uow),
    service: GenerationService = Depends(_get_service),
) -> GenerationRequestAccepted:
    """受理生成任务（准入：路线 VERIFIED + 引用归属 + 预算/配额引用）。"""
    authorize(
        principal,
        "GENERATION.REQUEST",
        resource=ResourceRef(
            resource_type="GenerationTask", project_id=body.project_id
        ),
    )
    task = await service.request_generation(
        project_id=body.project_id,
        shot_id=body.shot_id,
        route_id=body.route_id,
        shot_spec_ref=body.shot_spec_ref,
        visual_plan_ref=body.visual_plan_ref,
        constraint_snapshot_ref=body.constraint_snapshot_ref,
        budget_reservation_ref=body.budget_reservation_ref,
        quota_reservation_ref=body.quota_reservation_ref,
        dependency_refs=body.dependency_refs,
        principal=principal,
        retry_of=body.retry_of,
        rights_manifest_ref=body.rights_manifest_ref,
    )
    await uow.commit()
    return GenerationRequestAccepted(
        generation_task_id=task.entity_id,
        project_id=task.project_id,
        shot_id=task.shot_id,
        route_id=task.route_id,
        attempt=task.attempt,
        task_status=task.status.value,
    )
