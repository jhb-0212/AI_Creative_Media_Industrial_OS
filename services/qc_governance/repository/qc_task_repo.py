"""QCTask 的 SQLAlchemy 仓储（qc_governance schema）。

与 GenerationTask 仓储同构：``add`` / ``get_by_id`` / ``save``（乐观锁）+ 两条调度查询。
``find_by_generation_task`` 是入队幂等的判据：同一生成任务只能对应一个 QC 任务，
否则重复入队会让同一产物被检两次、报告互相覆盖语义。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.shared.port_errors import translate_write_error
from packages.shared.versioning import _orm_update_values, conditional_update
from services.project_planning.domain.enums import QCTaskStatus
from services.qc_governance.domain.qc_task import QCTask
from services.qc_governance.repository.orm import QCTaskModel


def _to_orm(task: QCTask) -> QCTaskModel:
    return QCTaskModel(
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
        qc_task_id=task.qc_task_id,
        project_id=task.project_id,
        shot_id=task.shot_id,
        asset_version_id=task.asset_version_id,
        generation_task_id=task.generation_task_id,
        quality_profile_version=task.quality_profile_version,
        lease_owner=task.lease_owner,
        lease_expires_at=task.lease_expires_at,
        lease_generation=task.lease_generation,
        attempt_count=task.attempt_count,
        qc_report_id=task.qc_report_id,
        organization_id=task.organization_id,
    )


def _to_domain(model: QCTaskModel) -> QCTask:
    try:
        status = QCTaskStatus(model.status)
    except ValueError as exc:
        raise DomainError(
            ErrorRegistry.INTERNAL_ERROR,
            detail=f"qc_tasks 存量枚举非法: status={model.status!r}",
        ) from exc
    return QCTask(
        entity_id=model.entity_id,
        created_by=model.created_by,
        tenant_id=model.tenant_id,
        schema_version=model.schema_version,
        entity_version=model.entity_version,
        owner_module=model.owner_module,
        created_at=model.created_at,
        updated_at=model.updated_at,
        audit_ref=model.audit_ref,
        qc_task_id=model.qc_task_id,
        project_id=model.project_id,
        shot_id=model.shot_id,
        asset_version_id=model.asset_version_id,
        generation_task_id=model.generation_task_id,
        quality_profile_version=model.quality_profile_version,
        status=status,
        lease_owner=model.lease_owner,
        lease_expires_at=model.lease_expires_at,
        lease_generation=model.lease_generation,
        attempt_count=model.attempt_count,
        qc_report_id=model.qc_report_id,
        organization_id=model.organization_id,
    )


class SqlAlchemyQCTaskRepository:
    """QC 待检任务的持久化仓储（带乐观锁）。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, task: QCTask) -> QCTask:
        """写入待检项；唯一冲突翻译成 ``DuplicateEntityError``（服务层据此判幂等）。

        为什么不能裸 ``flush``
        ---------------------
        ``uq_qc_governance_qc_tasks_generation_task`` 是「同一生成任务至多一个
        有效 QC 任务」的**库层事实**。但裸 flush 抛出的是 ``IntegrityError``：
        它同时覆盖唯一冲突（23505）、外键/CHECK（23xxx）与 RLS 拒绝写（42501），
        调用方拿到它只能报 500。后果不是「不够优雅」，而是两个真实故障：

        * relay 侧：并发重复投递撞唯一键 → 500 → 按失败重试 → 一路进死信，
          而事实上入队早已成功（``enqueue_idempotently`` 想收敛的正是这个场景，
          它只看得到 ``DomainError``，看不到裸的驱动异常）；
        * 人工恢复侧：本来「已经入队了」应回 409，却变成服务故障。

        SAVEPOINT + 端口契约翻译把「哪一类写失败」变成契约事实，与资产域
        QC 报告仓储（``sqlalchemy_qc_repo``）同一模式：用方只认识
        ``DuplicateEntityError`` / ``ReferentialIntegrityError``，不认识 SQLAlchemy。
        """
        try:
            async with self._session.begin_nested():
                self._session.add(_to_orm(task))
                await self._session.flush()
        except (IntegrityError, ProgrammingError) as exc:
            translate_write_error(exc, entity_id=task.entity_id)
        return task

    async def save(self, task: QCTask, expected_version: int) -> QCTask:
        await conditional_update(
            self._session,
            QCTaskModel,
            task.entity_id,
            expected_version,
            _orm_update_values(_to_orm(task)),
        )
        return task

    async def get_by_id(self, entity_id: str) -> QCTask | None:
        result = await self._session.execute(
            select(QCTaskModel).where(QCTaskModel.entity_id == entity_id)
        )
        model = result.scalar_one_or_none()
        return _to_domain(model) if model is not None else None

    async def get_by_id_for_update(self, entity_id: str) -> QCTask | None:
        """领取专用读取：行锁 + ``SKIP LOCKED`` 语义的**单行**版本。

        为什么按 id 领取也要锁：条件更新（``entity_version`` CAS）只保证「不会写
        覆盖别人的写」，不保证「两个 Worker 不会同时判断自己可以领取」。两个请求
        同时读到 QUEUED、都通过内存判定、再各自 CAS，只有一个能成功——但失败方
        已经把租约写进内存对象，且会向调用方返回一个「看起来失败了」的结果。
        行锁把这段判定串行化，让「读→判→写」在同一条任务上不可能交错。

        用 ``SKIP LOCKED`` 而不是 ``NOWAIT``：竞争时直接跳过，让调用方看到「这条
        已被别人占用」，而不是把等待时间变成请求延迟。
        """
        result = await self._session.execute(
            select(QCTaskModel)
            .where(QCTaskModel.entity_id == entity_id)
            .with_for_update(skip_locked=True)
        )
        model = result.scalar_one_or_none()
        return _to_domain(model) if model is not None else None

    async def find_by_generation_task(self, generation_task_id: str) -> QCTask | None:
        result = await self._session.execute(
            select(QCTaskModel).where(
                QCTaskModel.generation_task_id == generation_task_id
            )
        )
        model = result.scalar_one_or_none()
        return _to_domain(model) if model is not None else None

    async def next_claimable_for_claim(self) -> QCTask | None:
        """领取专用：FIFO 取最早 QUEUED，并加 ``FOR UPDATE SKIP LOCKED``。

        只读查询会让多个 Worker 读到同一条任务、在行锁上排队；``SKIP LOCKED``
        让被占住的行直接跳过。锁只覆盖领取一步，调用方必须同事务 claim 后立即提交。
        """
        result = await self._session.execute(
            select(QCTaskModel)
            .where(QCTaskModel.status == QCTaskStatus.QUEUED.value)
            .order_by(QCTaskModel.created_at.asc())
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        model = result.scalar_one_or_none()
        return _to_domain(model) if model is not None else None

    async def list_expired(self, now: datetime) -> list[QCTask]:
        result = await self._session.execute(
            select(QCTaskModel).where(
                QCTaskModel.status == QCTaskStatus.RUNNING.value,
                QCTaskModel.lease_expires_at.is_not(None),
                QCTaskModel.lease_expires_at < now,
            )
        )
        return [_to_domain(model) for model in result.scalars()]


__all__ = ["QCTask", "SqlAlchemyQCTaskRepository"]
