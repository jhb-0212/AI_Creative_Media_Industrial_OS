"""生成任务 API 路由（薄层，R16-D3）。"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
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
    """创建生成任务并（MVP 同步）推进终态。"""

    project_id: str = Field(..., min_length=1, max_length=64)
    shot_id: str = Field(..., min_length=1, max_length=64)
    shot_revision_id: str = Field(..., min_length=1, max_length=64)
    production_route_ref: str = Field(..., min_length=1, max_length=64)
    model_adapter_ref: str = Field(..., min_length=1, max_length=128)
    constraint_snapshot_id: str = Field(..., min_length=1, max_length=128)
    budget_ref: str = Field(..., min_length=1, max_length=128)
    prompt_ref: str = Field(..., min_length=1, max_length=200)
    result: Literal["SUCCEEDED", "FAILED"] = "SUCCEEDED"
    generation_task_id: str = Field("", max_length=64)


@router.post("", status_code=201)
async def request_generation(
    body: RequestGenerationRequest,
    principal: Principal = Depends(require_role("GENERATION.REQUEST")),
    uow: UnitOfWork = Depends(get_production_planning_tenant_uow),
    service: GenerationService = Depends(_get_service),
) -> dict[str, Any]:
    """创建生成任务并同步推进 SUCCEEDED / FAILED（MVP 同步执行模型）。

    REQUESTED 与终态同处一个事务：`advance_status` 的乐观锁更新与事件落库
    都在这一个 UoW 里，不存在「任务已创建但状态没推进」的中间态。
    """
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
        result=body.result,
        created_by=principal.subject_id,
        tenant_id=principal.tenant_id,
        generation_task_id=body.generation_task_id,
    )
    await uow.commit()
    return task.to_dict()


__all__ = ["router"]
