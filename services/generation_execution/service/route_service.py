"""ProductionRoute 应用服务（PRODUCTION.ROUTE.SELECT 选路用例）。

职责：
- ``select_route``（PRODUCTION.ROUTE.SELECT，MEDIUM，制片/TD/授权调度器）：
  为某镜头/任务选定生产路线。前置条件（动作矩阵）：
  1. 路线存在（NOT_FOUND）；
  2. 路线处于 VERIFIED（ACT-ROUTE-NEG-001：UNVERIFIED 路线拒绝）；
  3. 满足 rights/capability —— capability 由授权层强制（token_required +
     require_action + 项目范围 ResourceRef）；rights 的可校验载体
     （RightsManifest/RightsCase，ADR-R24 债务）尚未建模，消费闸在交付域
     D3「消费前权利校验」承担——这里不伪造 rights 判定，诚实留待债务收口。
- 选中发出契约登记的审计事件 PRODUCTION_ROUTE_SELECTED。

身份三要素（主体 / 租户 / 组织）只从 Principal 取（可信来源）。
选中不改变路线状态（UNVERIFIED→VERIFIED 的标定入口是适配器验证工作流，
契约未给 action）：选择是「使用事实」的审计记录，路线状态是「能力事实」。
"""

from __future__ import annotations

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.identity.principal import Principal
from packages.shared.outbox_writer import OutboxWriter
from services.generation_execution.domain.enums import ProductionRouteStatus
from services.generation_execution.domain.production_route import ProductionRoute
from services.generation_execution.repository.production_route_repo import (
    ProductionRouteRepository,
)


class RouteService:
    """生产路线选路用例。"""

    def __init__(
        self,
        route_repository: ProductionRouteRepository,
        outbox_writer: OutboxWriter,
    ) -> None:
        self._route_repository = route_repository
        self._outbox_writer = outbox_writer

    async def select_route(
        self,
        *,
        route_id: str,
        project_id: str,
        shot_id: str,
        principal: Principal,
    ) -> ProductionRoute:
        """为镜头选定生产路线（PRODUCTION.ROUTE.SELECT）。

        校验顺序刻意为先存在、后状态：幽灵路线 404、未验证路线 422，
        错误语义与错误码注册表一致（NOT_FOUND→404 / VALIDATION_ERROR→422）。
        """
        route_id = route_id.strip()
        project_id = project_id.strip()
        shot_id = shot_id.strip()
        if not route_id:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR, detail="route_id 不能为空"
            )
        if not project_id:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR, detail="project_id 不能为空"
            )
        if not shot_id:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR, detail="shot_id 不能为空"
            )

        route = await self._route_repository.get_by_id(route_id)
        if route is None:
            raise DomainError(
                ErrorRegistry.NOT_FOUND, detail=f"路线不存在: {route_id}"
            )
        if route.status is not ProductionRouteStatus.VERIFIED:
            # ACT-ROUTE-NEG-001：UNVERIFIED 路线拒绝（16.1 未验证能力不得
            # 作为生产路由的可用能力）。
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail=(
                    f"路线 {route_id} 状态为 {route.status.value}，"
                    "未验证路线不得被选中（须处于 VERIFIED）"
                ),
            )

        self._outbox_writer.record(
            aggregate_type="ProductionRoute",
            aggregate_id=route.entity_id,
            event_type="PRODUCTION_ROUTE_SELECTED",
            tenant_id=principal.tenant_id,
            payload={
                "route_id": route.entity_id,
                "route_type": route.route_type,
                "project_id": project_id,
                "shot_id": shot_id,
                "selected_by": principal.subject_id,
                "tenant_id": str(principal.tenant_id),
                "status": route.status.value,
            },
        )
        return route
