"""生产路线选择 API 路由（薄层，R16-D3）。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from packages.shared.unit_of_work import UnitOfWork
from services.identity_access.deps import (
    Principal,
    authorize_on_parent,
    get_production_planning_tenant_uow,
    require_resource_role,
    require_role,
)
from services.production_planning.repository.sqlalchemy_repos import (
    SqlAlchemyProductionRouteRepository,
)
from services.production_planning.service.production_route_service import (
    ProductionRouteService,
)
from services.project_planning.repository.sqlalchemy_outbox_writer import (
    SqlAlchemyOutboxWriter,
)

router = APIRouter(prefix="/production-routes", tags=["production-route"])


def _service(session: AsyncSession) -> ProductionRouteService:
    return ProductionRouteService(
        SqlAlchemyProductionRouteRepository(session),
        SqlAlchemyOutboxWriter(session),
    )


def _get_service(
    uow: UnitOfWork = Depends(get_production_planning_tenant_uow),
) -> ProductionRouteService:
    return _service(uow.session)


class SelectProductionRouteRequest(BaseModel):
    """选择一条已验证的能力路线（capability_ref 承载能力/权利证据）。"""

    project_id: str = Field(..., min_length=1, max_length=64)
    shot_id: str = Field(..., min_length=1, max_length=64)
    task_ref: str = Field(..., min_length=1, max_length=64)
    route_type: str = Field(..., min_length=1, max_length=32)
    capability_ref: str = Field(..., min_length=1, max_length=200)
    basis_ref: str = Field(..., min_length=1, max_length=200)
    route_id: str = Field("", max_length=64)


class RevokeProductionRouteRequest(BaseModel):
    """撤销依据（必填：没有依据的撤销无法被审计复核）。"""

    revocation_ref: str = Field(..., min_length=1, max_length=128)


@router.post("", status_code=201)
async def select_production_route(
    body: SelectProductionRouteRequest,
    principal: Principal = Depends(require_role("PRODUCTION.ROUTE.SELECT")),
    uow: UnitOfWork = Depends(get_production_planning_tenant_uow),
    service: ProductionRouteService = Depends(_get_service),
) -> dict[str, Any]:
    """为某镜头/任务选定一条已验证路线，创建即 VERIFIED。

    归属判定对象是父项目（路线尚不存在）：先证明「这个项目归你」，再落记录，
    否则可以先在别人项目里写数据，再借后续流程读取。
    """
    await authorize_on_parent(
        "PRODUCTION.ROUTE.SELECT",
        principal,
        uow.session,
        parent_type="Project",
        parent_id=body.project_id,
    )
    route = await service.select_route(
        project_id=body.project_id,
        shot_id=body.shot_id,
        task_ref=body.task_ref,
        route_type=body.route_type,
        capability_ref=body.capability_ref,
        basis_ref=body.basis_ref,
        created_by=principal.subject_id,
        tenant_id=principal.tenant_id,
        route_id=body.route_id,
    )
    await uow.commit()
    return route.to_dict()


@router.post("/{entity_id}/revoke")
async def revoke_production_route(
    entity_id: str,
    body: RevokeProductionRouteRequest,
    principal: Principal = Depends(
        # 资源级判定：目标路线必须在本主体的租户/组织作用域内（归属经 projects 解析）。
        require_resource_role(
            "PRODUCTION.ROUTE.SELECT",
            resource_type="ProductionRoute",
            param_name="entity_id",
            uow_dep=get_production_planning_tenant_uow,
        )
    ),
    uow: UnitOfWork = Depends(get_production_planning_tenant_uow),
    service: ProductionRouteService = Depends(_get_service),
) -> dict[str, Any]:
    """撤销路线选择（VERIFIED → REVOKED，终态不可原地复活）。"""
    route = await service.revoke_route(
        entity_id=entity_id,
        revoked_by=principal.subject_id,
        revocation_ref=body.revocation_ref,
        tenant_id=principal.tenant_id,
    )
    await uow.commit()
    return route.to_dict()


__all__ = ["router"]
