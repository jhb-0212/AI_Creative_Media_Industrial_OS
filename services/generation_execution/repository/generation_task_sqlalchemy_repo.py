"""GenerationTask SQLAlchemy 仓储实现（数据所有权：generation_execution 模块专属写入口）。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.shared.persistence import translate_integrity_errors
from packages.shared.versioning import conditional_update, conditional_update_where
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
        lease_owner=model.lease_owner,
        lease_expires_at=model.lease_expires_at,
        started_at=model.started_at,
        completed_at=model.completed_at,
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
        lease_owner=task.lease_owner,
        lease_expires_at=task.lease_expires_at,
        started_at=task.started_at,
        completed_at=task.completed_at,
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
                # 租约与转移伴随元数据：领取/续租/终态的唯一写入口。
                "lease_owner": task.lease_owner,
                "lease_expires_at": task.lease_expires_at,
                "started_at": task.started_at,
                "completed_at": task.completed_at,
            },
        )
        return task

    async def claim_pending(
        self,
        *,
        actor: str,
        worker_id: str,
        lease_expires_at: datetime,
        now: datetime,
        limit: int,
    ) -> list[str]:
        """一条语句完成"挑选 + 占有"（``FOR UPDATE SKIP LOCKED``，R06 同范式）。

        子查询锁住候选行并跳过已被其它事务锁住的行，UPDATE 直接把它们改成
        RUNNING 并写上本次租约。并发实例因此天然分到不同批次，不会出现
        "六个 worker 抢同一批 16 条、只有一个成功"的空转。

        RLS 同时作用于子查询与 UPDATE：项目范围之外的 PENDING 任务既不会被
        选中，也不会被改写。
        """
        stmt = text(
            """
            WITH candidates AS (
                SELECT entity_id
                FROM generation_execution.generation_tasks
                WHERE status = 'PENDING'
                ORDER BY created_at, entity_id
                LIMIT :batch
                FOR UPDATE SKIP LOCKED
            )
            UPDATE generation_execution.generation_tasks AS t
            SET status = 'RUNNING',
                lease_owner = :worker_id,
                lease_expires_at = :expires_at,
                started_at = :now,
                entity_version = t.entity_version + 1,
                updated_at = :now,
                audit_ref = :audit_ref
            FROM candidates AS c
            WHERE t.entity_id = c.entity_id
            RETURNING t.entity_id
            """
        )
        rows = await self._session.execute(
            stmt,
            {
                "batch": limit,
                "worker_id": worker_id,
                "expires_at": lease_expires_at,
                "now": now,
                "audit_ref": f"claim:{actor}@{worker_id}",
            },
        )
        return [str(row[0]) for row in rows]

    async def list_expired_leases(
        self, limit: int, *, now: datetime
    ) -> list[GenerationTask]:
        stmt = (
            select(GenerationTaskModel)
            .where(
                GenerationTaskModel.status == GenerationTaskStatus.RUNNING.value,
                GenerationTaskModel.lease_expires_at.is_not(None),
                GenerationTaskModel.lease_expires_at < now,
            )
            # 先到期先回收：崩溃最久的任务优先回到执行轨道。
            .order_by(GenerationTaskModel.lease_expires_at)
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return [_to_domain(model) for model in result.scalars()]

    async def take_over(
        self,
        task_id: str,
        *,
        worker_id: str,
        expires_at: datetime,
        now: datetime,
    ) -> bool:
        """谓词 CAS 接管：状态 RUNNING + 租约已过期，一次 UPDATE 内判定。"""
        affected = await conditional_update_where(
            self._session,
            GenerationTaskModel,
            [
                GenerationTaskModel.entity_id == task_id,
                GenerationTaskModel.status == GenerationTaskStatus.RUNNING.value,
                GenerationTaskModel.lease_expires_at.is_not(None),
                GenerationTaskModel.lease_expires_at < now,
            ],
            {
                "lease_owner": worker_id,
                "lease_expires_at": expires_at,
                "updated_at": now,
                "audit_ref": f"takeover:{worker_id}",
            },
        )
        return affected == 1
