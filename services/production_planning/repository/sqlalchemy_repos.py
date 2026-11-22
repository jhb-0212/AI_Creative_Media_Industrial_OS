"""生产路线与生成任务的 SQLAlchemy 仓储（R16，project_planning schema 过渡期）。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.shared.versioning import _orm_update_values, conditional_update
from services.production_planning.domain.generation_task import GenerationTask
from services.production_planning.domain.production_route import ProductionRoute
from services.production_planning.repository.orm import (
    GenerationTaskModel,
    ProductionRouteModel,
)
from services.project_planning.domain.enums import (
    GenerationTaskStatus,
    ProductionRouteStatus,
)


def _route_to_orm(route: ProductionRoute) -> ProductionRouteModel:
    return ProductionRouteModel(
        entity_id=route.entity_id,
        entity_type=route.ENTITY_TYPE,
        schema_version=route.schema_version,
        entity_version=route.entity_version,
        status=route.status.value,
        owner_module=route.owner_module,
        tenant_id=route.tenant_id,
        created_at=route.created_at,
        created_by=route.created_by,
        updated_at=route.updated_at,
        audit_ref=route.audit_ref,
        route_id=route.route_id,
        project_id=route.project_id,
        shot_id=route.shot_id,
        task_ref=route.task_ref,
        route_type=route.route_type,
        capability_ref=route.capability_ref,
        basis_ref=route.basis_ref,
    )


def _route_to_domain(model: ProductionRouteModel) -> ProductionRoute:
    try:
        status = ProductionRouteStatus(model.status)
    except ValueError as exc:
        raise DomainError(
            ErrorRegistry.INTERNAL_ERROR,
            detail=f"production_routes 存量枚举非法: status={model.status!r}",
        ) from exc
    return ProductionRoute(
        entity_id=model.entity_id,
        created_by=model.created_by,
        tenant_id=model.tenant_id,
        schema_version=model.schema_version,
        entity_version=model.entity_version,
        owner_module=model.owner_module,
        created_at=model.created_at,
        updated_at=model.updated_at,
        audit_ref=model.audit_ref,
        route_id=model.route_id,
        project_id=model.project_id,
        shot_id=model.shot_id,
        task_ref=model.task_ref,
        route_type=model.route_type,
        capability_ref=model.capability_ref,
        basis_ref=model.basis_ref,
        status=status,
    )


class SqlAlchemyProductionRouteRepository:
    """ProductionRoute 的持久化仓储。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, route: ProductionRoute) -> ProductionRoute:
        self._session.add(_route_to_orm(route))
        # flush 让 INSERT 先于后续乐观锁 UPDATE 落到事务里（autoflush=False，
        # 不显式 flush 时同事务内的 UPDATE 会命中 0 行而误判并发冲突）。
        await self._session.flush()
        return route

    async def save(self, route: ProductionRoute, expected_version: int) -> ProductionRoute:
        await conditional_update(
            self._session,
            ProductionRouteModel,
            route.entity_id,
            expected_version,
            _orm_update_values(_route_to_orm(route)),
        )
        return route

    async def get_by_id(self, entity_id: str) -> ProductionRoute | None:
        result = await self._session.execute(
            select(ProductionRouteModel).where(ProductionRouteModel.entity_id == entity_id)
        )
        model = result.scalar_one_or_none()
        return _route_to_domain(model) if model is not None else None


def _task_to_orm(task: GenerationTask) -> GenerationTaskModel:
    return GenerationTaskModel(
        entity_id=task.entity_id,
        entity_type=task.ENTITY_TYPE,
        schema_version=task.schema_version,
        entity_version=task.entity_version,
        status=task.status.value,
        owner_module=task.owner_module,
        tenant_id=task.tenant_id,
        created_at=task.created_at,
        created_by=task.created_by,
        updated_at=task.updated_at,
        audit_ref=task.audit_ref,
        generation_task_id=task.generation_task_id,
        project_id=task.project_id,
        shot_id=task.shot_id,
        shot_revision_id=task.shot_revision_id,
        production_route_ref=task.production_route_ref,
        model_adapter_ref=task.model_adapter_ref,
        constraint_snapshot_id=task.constraint_snapshot_id,
        budget_ref=task.budget_ref,
        prompt_ref=task.prompt_ref,
        lease_owner=task.lease_owner,
        lease_expires_at=task.lease_expires_at,
        attempt_count=task.attempt_count,
        asset_version_id=task.asset_version_id,
        quality_profile_version=task.quality_profile_version,
    )


def _task_to_domain(model: GenerationTaskModel) -> GenerationTask:
    try:
        status = GenerationTaskStatus(model.status)
    except ValueError as exc:
        raise DomainError(
            ErrorRegistry.INTERNAL_ERROR,
            detail=f"generation_tasks 存量枚举非法: status={model.status!r}",
        ) from exc
    return GenerationTask(
        entity_id=model.entity_id,
        created_by=model.created_by,
        tenant_id=model.tenant_id,
        schema_version=model.schema_version,
        entity_version=model.entity_version,
        owner_module=model.owner_module,
        created_at=model.created_at,
        updated_at=model.updated_at,
        audit_ref=model.audit_ref,
        generation_task_id=model.generation_task_id,
        project_id=model.project_id,
        shot_id=model.shot_id,
        shot_revision_id=model.shot_revision_id,
        production_route_ref=model.production_route_ref,
        model_adapter_ref=model.model_adapter_ref,
        constraint_snapshot_id=model.constraint_snapshot_id,
        budget_ref=model.budget_ref,
        prompt_ref=model.prompt_ref,
        status=status,
        lease_owner=model.lease_owner,
        lease_expires_at=model.lease_expires_at,
        attempt_count=model.attempt_count,
        asset_version_id=model.asset_version_id,
        quality_profile_version=model.quality_profile_version,
    )


class SqlAlchemyGenerationTaskRepository:
    """GenerationTask 的持久化仓储。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, task: GenerationTask) -> GenerationTask:
        self._session.add(_task_to_orm(task))
        await self._session.flush()
        return task

    async def save(self, task: GenerationTask, expected_version: int) -> GenerationTask:
        await conditional_update(
            self._session,
            GenerationTaskModel,
            task.entity_id,
            expected_version,
            _orm_update_values(_task_to_orm(task)),
        )
        return task

    async def get_by_id(self, entity_id: str) -> GenerationTask | None:
        result = await self._session.execute(
            select(GenerationTaskModel).where(GenerationTaskModel.entity_id == entity_id)
        )
        model = result.scalar_one_or_none()
        return _task_to_domain(model) if model is not None else None

    async def next_claimable(self) -> GenerationTask | None:
        """最早创建的 REQUESTED 任务（FIFO，避免新任务插队把老任务饿死）。"""
        result = await self._session.execute(
            select(GenerationTaskModel)
            .where(GenerationTaskModel.status == GenerationTaskStatus.REQUESTED.value)
            .order_by(GenerationTaskModel.created_at.asc())
            .limit(1)
        )
        model = result.scalar_one_or_none()
        return _task_to_domain(model) if model is not None else None

    async def list_expired(self, now: datetime) -> list[GenerationTask]:
        """租约已过期但仍停在 RUNNING 的任务（崩溃 Worker 留下的）。"""
        result = await self._session.execute(
            select(GenerationTaskModel).where(
                GenerationTaskModel.status == GenerationTaskStatus.RUNNING.value,
                GenerationTaskModel.lease_expires_at.is_not(None),
                GenerationTaskModel.lease_expires_at < now,
            )
        )
        return [_task_to_domain(model) for model in result.scalars()]

    async def list_succeeded_with_asset(self, limit: int = 50) -> list[GenerationTask]:
        """成功且登记了产物引用的任务（QC 漏入队对账的数据源）。

        只取带 ``asset_version_id`` 的行：没有产物引用的历史成功任务无法映射到
        待检资产，扫描它们只会制造无对象的入队请求。
        """
        result = await self._session.execute(
            select(GenerationTaskModel)
            .where(
                GenerationTaskModel.status == GenerationTaskStatus.SUCCEEDED.value,
                GenerationTaskModel.asset_version_id != "",
            )
            .order_by(GenerationTaskModel.updated_at.asc())
            .limit(limit)
        )
        return [_task_to_domain(model) for model in result.scalars()]


__all__ = [
    "SqlAlchemyGenerationTaskRepository",
    "SqlAlchemyProductionRouteRepository",
]
