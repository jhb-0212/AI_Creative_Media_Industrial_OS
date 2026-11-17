"""ProductionRoute API 路由（薄层，只做协议适配与 DTO 转换）。

契约对齐（动作矩阵 / openapi.draft /actions/production.route.select）：
- PRODUCTION.ROUTE.SELECT：为镜头选定生产路线（MEDIUM，制片/TD/授权调度器）；
  路线须处于 VERIFIED 且满足 rights/capability（ACT-ROUTE-NEG-001）。
- 请求/响应走统一信封与错误信封；业务逻辑下沉到 RouteService。
- 挂载在契约冻结路径 /actions/production.route.select 上（与 openapi.draft
  同路径，来源可追溯，与 constraint-pack 的资源式路径不同——本动作语义是
  「选路」而非「资源 CRUD」，动作式路径更贴切）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from packages.identity.authorization import ResourceRef
from packages.identity.enforcement import authorize
from packages.shared.outbox_writer import SqlAlchemyOutboxWriter
from packages.shared.unit_of_work import UnitOfWork
from services.generation_execution.repository.production_route_sqlalchemy_repo import (
    SqlAlchemyProductionRouteRepository,
)
from services.generation_execution.service.route_service import RouteService
from services.identity_access.deps import (
    Principal,
    get_generation_uow,
    require_action,
)

router = APIRouter(prefix="/actions", tags=["generation-execution"])


class SelectRouteRequest(BaseModel):
    """选路请求体（身份不在请求体，由 Principal 提供）。"""

    route_id: str = Field(..., min_length=1, max_length=64)
    project_id: str = Field(..., min_length=1, max_length=64)
    shot_id: str = Field(..., min_length=1, max_length=64)


class RouteSelectionAccepted(BaseModel):
    """选路受理回执（对齐 openapi.draft ActionAccepted）。"""

    status: str = "ACCEPTED"
    audit_event: str = "PRODUCTION_ROUTE_SELECTED"
    route_id: str
    project_id: str
    shot_id: str


def _build_service(session: Any) -> RouteService:
    """服务装配：RLS 会话 + 路线仓储端口 + Outbox 写入端口。"""
    return RouteService(
        SqlAlchemyProductionRouteRepository(session),
        SqlAlchemyOutboxWriter(session),
    )


def _get_service(uow: UnitOfWork = Depends(get_generation_uow)) -> RouteService:
    """写用例服务装配（提交点由路由显式控制）。"""
    return _build_service(uow.session)


@router.post("/production.route.select", response_model=RouteSelectionAccepted)
async def select_production_route(
    body: SelectRouteRequest,
    principal: Principal = Depends(require_action("PRODUCTION.ROUTE.SELECT")),
    uow: UnitOfWork = Depends(get_generation_uow),
    service: RouteService = Depends(_get_service),
) -> RouteSelectionAccepted:
    """为镜头选定生产路线（UNVERIFIED 路线拒绝：ACT-ROUTE-NEG-001）。"""
    authorize(
        principal,
        "PRODUCTION.ROUTE.SELECT",
        resource=ResourceRef(
            resource_type="ProductionRoute", project_id=body.project_id
        ),
    )
    route = await service.select_route(
        route_id=body.route_id,
        project_id=body.project_id,
        shot_id=body.shot_id,
        principal=principal,
    )
    await uow.commit()
    return RouteSelectionAccepted(
        route_id=route.entity_id,
        project_id=body.project_id,
        shot_id=body.shot_id,
    )
