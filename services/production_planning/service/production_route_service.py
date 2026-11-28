"""ProductionRoute 应用服务（生产路线选择，R16-D3）。"""

from __future__ import annotations

from typing import Any, cast
from uuid import UUID, uuid4

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.shared.quality_profile import UNIFIED_HIGH_QUALITY
from services.production_planning.domain.production_route import ProductionRoute
from services.project_planning.domain.enums import ProductionRouteStatus
from services.project_planning.domain.state_machine import PRODUCTION_ROUTE_SELECT_ACTION
from services.project_planning.service.transition import advance_status


class ProductionRouteService:
    """选择一条已验证的能力路线（创建即 VERIFIED）。

    两个入口同属一个动作（``PRODUCTION.ROUTE.SELECT``）：
    - ``select_route``：落一条 VERIFIED 记录；
    - ``revoke_route``：走治理边 VERIFIED → REVOKED（撤销依据必填）。

    撤销为什么归 SELECT 而不是另开动作：状态机里这条边的 ``owner_action`` 就是
    SELECT（``state_machines.yaml#ProductionRoute``）。另造一个动作键会让「谁能撤销」
    与契约声明的边所有权分离，等于在准入凭据之外又开一个入口。
    """

    def __init__(self, repository: Any, outbox_writer: Any) -> None:
        self._repository = repository
        self._outbox_writer = outbox_writer

    async def select_route(
        self,
        *,
        project_id: str,
        shot_id: str,
        task_ref: str,
        route_type: str,
        capability_ref: str,
        basis_ref: str,
        created_by: str,
        tenant_id: UUID,
        route_id: str = "",
    ) -> ProductionRoute:
        route = ProductionRoute(
            entity_id=str(uuid4()),
            created_by=created_by,
            tenant_id=tenant_id,
            route_id=route_id,
            project_id=project_id,
            shot_id=shot_id,
            task_ref=task_ref,
            route_type=route_type,
            capability_ref=capability_ref,
            basis_ref=basis_ref,
            # C4：路线创建即绑定契约裁定的唯一质量策略。不在请求体开放该字段，
            # 也不查库「取当前版本」——权威源是契约单值，调用方无可自报余地。
            quality_profile_version=UNIFIED_HIGH_QUALITY,
        )
        await self._repository.add(route)
        self._outbox_writer.record(
            aggregate_type="ProductionRoute",
            aggregate_id=route.entity_id,
            tenant_id=tenant_id,
            event_type="PRODUCTION_ROUTE_SELECTED",
            payload={
                "route_id": route.route_id,
                "project_id": route.project_id,
                "shot_id": route.shot_id,
                "task_ref": route.task_ref,
                "route_type": route.route_type,
                "capability_ref": route.capability_ref,
                "created_by": created_by,
                "tenant_id": str(tenant_id),
            },
        )
        return route

    async def revoke_route(
        self,
        *,
        entity_id: str,
        revoked_by: str,
        revocation_ref: str,
        tenant_id: UUID,
    ) -> ProductionRoute:
        """撤销一条路线选择（VERIFIED → REVOKED，终态不可原地复活）。

        ``revocation_ref`` 必填：撤销是「某条能力/权利证据失效」的结论，没有依据的
        撤销无法被审计复核——而这正是治理边需要留痕的唯一原因。
        """
        if not revocation_ref or not revocation_ref.strip():
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail="撤销必须给出撤销依据（revocation_ref 不能为空）",
            )

        route = await self._repository.get_by_id(entity_id)
        if route is None:
            raise DomainError(ErrorRegistry.NOT_FOUND, detail=f"生产路线不存在: {entity_id}")

        await advance_status(
            route,
            ProductionRouteStatus.REVOKED,
            repository=self._repository,
            outbox_writer=self._outbox_writer,
            aggregate_type="ProductionRoute",
            event_type="PRODUCTION_ROUTE_REVOKED",
            tenant_id=tenant_id,
            payload={
                "route_id": route.route_id,
                "project_id": route.project_id,
                "shot_id": route.shot_id,
                "revocation_ref": revocation_ref,
                "revoked_by": revoked_by,
                "tenant_id": str(tenant_id),
            },
            resource_type="ProductionRoute",
            owner_action=PRODUCTION_ROUTE_SELECT_ACTION,
            conflict_detail="生产路线并发冲突，请基于最新状态重试",
            actor_field="revoked_by",
            actor=revoked_by,
        )
        return cast("ProductionRoute", route)


__all__ = ["ProductionRouteService"]
