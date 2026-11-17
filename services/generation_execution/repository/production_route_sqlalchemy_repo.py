"""ProductionRoute SQLAlchemy 仓储实现（数据所有权：generation_execution 模块专属写入口）。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.shared.persistence import translate_integrity_errors
from services.generation_execution.domain.enums import ProductionRouteStatus
from services.generation_execution.domain.production_route import ProductionRoute
from services.generation_execution.repository.orm import ProductionRouteModel
from services.generation_execution.repository.production_route_repo import (
    ProductionRouteRepository,
)


def _to_domain(model: ProductionRouteModel) -> ProductionRoute:
    """ORM -> 领域模型（枚举反序列化防御：非法值降级为结构化 DomainError）。"""
    try:
        status = ProductionRouteStatus(model.status)
    except ValueError as exc:
        raise DomainError(
            ErrorRegistry.INTERNAL_ERROR,
            detail=f"production_routes 存量取值非法: status={model.status!r}",
        ) from exc
    return ProductionRoute(
        entity_id=model.entity_id,
        route_type=model.route_type,
        supported_control_signals=model.supported_control_signals,
        required_appearance_refs=model.required_appearance_refs,
        qc_dimensions=model.qc_dimensions,
        capability_refs=model.capability_refs,
        status=status,
        created_by=model.created_by,
        tenant_id=model.tenant_id,
        organization_id=model.organization_id,
        schema_version=model.schema_version,
        entity_version=model.entity_version,
        owner_module=model.owner_module,
        created_at=model.created_at,
        updated_at=model.updated_at,
        audit_ref=model.audit_ref,
    )


def _to_orm(route: ProductionRoute) -> ProductionRouteModel:
    """领域模型 -> ORM。"""
    return ProductionRouteModel(
        entity_id=route.entity_id,
        entity_type=route.ENTITY_TYPE,
        schema_version=route.schema_version,
        entity_version=route.entity_version,
        status=route.status.value,
        owner_module=route.owner_module,
        tenant_id=route.tenant_id,
        organization_id=route.organization_id,
        created_at=route.created_at,
        created_by=route.created_by,
        updated_at=route.updated_at,
        audit_ref=route.audit_ref,
        route_type=route.route_type,
        supported_control_signals=route.supported_control_signals,
        required_appearance_refs=route.required_appearance_refs,
        qc_dimensions=route.qc_dimensions,
        capability_refs=route.capability_refs,
    )


class SqlAlchemyProductionRouteRepository(ProductionRouteRepository):
    """基于 SQLAlchemy 异步会话的仓储实现。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, route: ProductionRoute) -> None:
        # INSERT 语义 + SAVEPOINT：并发重复登记由主键约束捕获。
        with translate_integrity_errors(entity=f"route_id={route.entity_id}"):
            async with self._session.begin_nested():
                self._session.add(_to_orm(route))

    async def get_by_id(self, entity_id: str) -> ProductionRoute | None:
        stmt = select(ProductionRouteModel).where(
            ProductionRouteModel.entity_id == entity_id
        )
        result = await self._session.execute(stmt)
        model = result.scalar_one_or_none()
        return _to_domain(model) if model is not None else None
