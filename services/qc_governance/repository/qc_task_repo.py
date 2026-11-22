"""QCTask 的 SQLAlchemy 仓储（qc_governance schema）。

与 GenerationTask 仓储同构：``add`` / ``get_by_id`` / ``save``（乐观锁）+ 两条调度查询。
``find_by_generation_task`` 是入队幂等的判据：同一生成任务只能对应一个 QC 任务，
否则重复入队会让同一产物被检两次、报告互相覆盖语义。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.errors.error_codes import DomainError, ErrorRegistry
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
        attempt_count=task.attempt_count,
        qc_report_id=task.qc_report_id,
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
        attempt_count=model.attempt_count,
        qc_report_id=model.qc_report_id,
    )


class SqlAlchemyQCTaskRepository:
    """QC 待检任务的持久化仓储（带乐观锁）。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, task: QCTask) -> QCTask:
        self._session.add(_to_orm(task))
        await self._session.flush()
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

    async def find_by_generation_task(self, generation_task_id: str) -> QCTask | None:
        result = await self._session.execute(
            select(QCTaskModel).where(
                QCTaskModel.generation_task_id == generation_task_id
            )
        )
        model = result.scalar_one_or_none()
        return _to_domain(model) if model is not None else None

    async def next_claimable(self) -> QCTask | None:
        result = await self._session.execute(
            select(QCTaskModel)
            .where(QCTaskModel.status == QCTaskStatus.QUEUED.value)
            .order_by(QCTaskModel.created_at.asc())
            .limit(1)
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
