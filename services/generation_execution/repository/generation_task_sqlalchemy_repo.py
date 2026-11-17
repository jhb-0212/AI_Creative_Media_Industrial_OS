"""GenerationTask SQLAlchemy 仓储实现（数据所有权：generation_execution 模块专属写入口）。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.shared.persistence import translate_integrity_errors
from packages.shared.versioning import conditional_update
from services.generation_execution.domain.enums import GenerationTaskStatus
from services.generation_execution.domain.generation_task import GenerationTask
from services.generation_execution.repository.generation_task_repo import (
    GenerationTaskRepository,
)
from services.generation_execution.repository.orm import GenerationTaskModel


def _to_domain(model: GenerationTaskModel) -> GenerationTask:
    """ORM -> 领域模型（枚举反序列化防御：非法值降级为结构化 DomainError）。"""
    try:
        status = GenerationTaskStatus(model.status)
    except ValueError as exc:
        raise DomainError(
            ErrorRegistry.INTERNAL_ERROR,
            detail=f"generation_tasks 存量取值非法: status={model.status!r}",
        ) from exc
    return GenerationTask(
        entity_id=model.entity_id,
        project_id=model.project_id,
        shot_id=model.shot_id,
        route_id=model.route_id,
        shot_spec_ref=model.shot_spec_ref,
        visual_plan_ref=model.visual_plan_ref,
        constraint_snapshot_ref=model.constraint_snapshot_ref,
        budget_reservation_ref=model.budget_reservation_ref,
        quota_reservation_ref=model.quota_reservation_ref,
        dependency_refs=list(model.dependency_refs),
        attempt=model.attempt,
        retry_of=model.retry_of,
        rights_manifest_ref=model.rights_manifest_ref,
        status=status,
        result_ref=model.result_ref,
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


def _to_orm(task: GenerationTask) -> GenerationTaskModel:
    """领域模型 -> ORM。"""
    return GenerationTaskModel(
        entity_id=task.entity_id,
        entity_type=task.ENTITY_TYPE,
        schema_version=task.schema_version,
        entity_version=task.entity_version,
        status=task.status.value,
        owner_module=task.owner_module,
        tenant_id=task.tenant_id,
        organization_id=task.organization_id,
        created_at=task.created_at,
        created_by=task.created_by,
        updated_at=task.updated_at,
        audit_ref=task.audit_ref,
        project_id=task.project_id,
        shot_id=task.shot_id,
        route_id=task.route_id,
        shot_spec_ref=task.shot_spec_ref,
        visual_plan_ref=task.visual_plan_ref,
        constraint_snapshot_ref=task.constraint_snapshot_ref,
        budget_reservation_ref=task.budget_reservation_ref,
        quota_reservation_ref=task.quota_reservation_ref,
        dependency_refs=task.dependency_refs,
        attempt=task.attempt,
        retry_of=task.retry_of,
        rights_manifest_ref=task.rights_manifest_ref,
        result_ref=task.result_ref,
    )


class SqlAlchemyGenerationTaskRepository(GenerationTaskRepository):
    """基于 SQLAlchemy 异步会话的仓储实现。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, task: GenerationTask) -> None:
        # INSERT 语义 + SAVEPOINT：并发重复登记由主键约束捕获。
        with translate_integrity_errors(entity=f"generation_task_id={task.entity_id}"):
            async with self._session.begin_nested():
                self._session.add(_to_orm(task))

    async def get_by_id(self, entity_id: str) -> GenerationTask | None:
        stmt = select(GenerationTaskModel).where(
            GenerationTaskModel.entity_id == entity_id
        )
        result = await self._session.execute(stmt)
        model = result.scalar_one_or_none()
        return _to_domain(model) if model is not None else None

    async def save(
        self, task: GenerationTask, *, expected_version: int
    ) -> GenerationTask:
        """乐观并发更新：复用共享 ``conditional_update``（单一乐观锁机制）。

        版本推进统一由 helper 负责（expected+1，不手写口径）；状态/审计/
        产出引用是任务生命周期仅有的可变列，不可变内容列由数据库守卫
        触发器兜底。版本被抢先抛 ``ConcurrentModificationError``（DomainError
        子族，worker 按幂等跳过处理）。
        """
        await conditional_update(
            self._session,
            GenerationTaskModel,
            task.entity_id,
            expected_version,
            {
                "status": task.status.value,
                "updated_at": task.updated_at,
                "audit_ref": task.audit_ref,
                "result_ref": task.result_ref,
            },
        )
        return task

    async def list_pending(self, limit: int) -> list[GenerationTask]:
        stmt = (
            select(GenerationTaskModel)
            .where(GenerationTaskModel.status == GenerationTaskStatus.PENDING.value)
            .order_by(GenerationTaskModel.created_at)
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return [_to_domain(model) for model in result.scalars()]
