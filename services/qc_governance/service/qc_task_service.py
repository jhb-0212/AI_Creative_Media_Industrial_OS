"""QCTask 应用服务（QC 待检队列 + 租约，Phase 3）。

三条硬约束：
1. 入队幂等：同一 ``generation_task_id`` 只能对应一个 QC 任务（仓储唯一索引 +
   显式冲突检查），避免重复入队让同一产物被检两次；
2. 租约：领取/续租/判定/回收全部走本服务，状态事实与事件由唯一入口
   ``advance_status`` / ``save_versioned_entity`` 写入，没有第二处改状态的地方；
3. 判定必须有报告：``qc_report_id`` 为空直接拒绝——「无证据 PASS」是最危险的假绿。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID, uuid4

from packages.errors.error_codes import DomainError, ErrorRegistry
from services.project_planning.domain.enums import QCTaskStatus
from services.project_planning.service.transition import (
    advance_status,
    save_versioned_entity,
)
from services.qc_governance.domain.qc_task import QCTask
from services.qc_governance.domain.state_machine import (
    QC_TASK_PROCESS_ACTION,
)


def _now() -> datetime:
    return datetime.now(UTC)


class QCTaskService:
    def __init__(self, repository: Any, outbox_writer: Any) -> None:
        self._repository = repository
        self._outbox_writer = outbox_writer

    async def enqueue(
        self,
        *,
        project_id: str,
        asset_version_id: str,
        generation_task_id: str,
        quality_profile_version: str,
        created_by: str,
        tenant_id: UUID,
        shot_id: str = "",
        qc_task_id: str = "",
    ) -> QCTask:
        """生成终态入队：创建 QUEUED。重复入队拒绝（业务冲突，不是 500）。"""
        existing = await self._repository.find_by_generation_task(generation_task_id)
        if existing is not None:
            raise DomainError(
                ErrorRegistry.IDEMPOTENCY_CONFLICT,
                detail=f"生成任务 {generation_task_id} 已入队（QC 任务 {existing.entity_id}）",
            )
        task = QCTask(
            entity_id=str(uuid4()),
            created_by=created_by,
            tenant_id=tenant_id,
            qc_task_id=qc_task_id,
            project_id=project_id,
            shot_id=shot_id,
            asset_version_id=asset_version_id,
            generation_task_id=generation_task_id,
            quality_profile_version=quality_profile_version,
        )
        await self._repository.add(task)
        self._outbox_writer.record(
            aggregate_type="QCTask",
            aggregate_id=task.entity_id,
            tenant_id=tenant_id,
            event_type="QC_TASK_QUEUED",
            payload={
                "qc_task_id": task.qc_task_id,
                "project_id": task.project_id,
                "asset_version_id": task.asset_version_id,
                "generation_task_id": task.generation_task_id,
                "quality_profile_version": task.quality_profile_version,
                "tenant_id": str(tenant_id),
            },
        )
        return task

    async def _load(self, task_id: str) -> QCTask:
        task = await self._repository.get_by_id(task_id)
        if task is None:
            raise DomainError(ErrorRegistry.NOT_FOUND, detail=f"QC 任务不存在: {task_id}")
        return task

    async def claim(
        self,
        *,
        task_id: str,
        worker_id: str,
        lease_seconds: int,
    ) -> QCTask:
        task = await self._load(task_id)
        expires = _now() + timedelta(seconds=lease_seconds)
        await advance_status(
            task,
            QCTaskStatus.RUNNING,
            repository=self._repository,
            outbox_writer=self._outbox_writer,
            aggregate_type="QCTask",
            event_type="QC_TASK_STARTED",
            tenant_id=task.tenant_id,
            payload={
                "qc_task_id": task.qc_task_id,
                "worker_id": worker_id,
                "lease_expires_at": expires.isoformat(),
                "attempt_count": task.attempt_count + 1,
            },
            resource_type="QCTask",
            owner_action=QC_TASK_PROCESS_ACTION,
            conflict_detail="QC 任务已被其他 Worker 领取，请勿重复领取",
            actor_field="worker_id",
            actor=worker_id,
            domain_operation=lambda audit_ref, action: task.claim(
                worker_id, expires, audit_ref
            ),
        )
        return task

    async def renew(
        self,
        *,
        task_id: str,
        worker_id: str,
        lease_seconds: int,
    ) -> QCTask:
        task = await self._load(task_id)
        task.require_lease_owner(worker_id)
        expires = _now() + timedelta(seconds=lease_seconds)
        await save_versioned_entity(
            task,
            mutation=lambda audit_ref, action: task.renew(
                worker_id, expires, audit_ref
            ),
            audit_ref=f"qc-lease-renew:{uuid4()}",
            repository=self._repository,
            outbox_writer=self._outbox_writer,
            aggregate_type="QCTask",
            aggregate_id=task.entity_id,
            event_type="QC_TASK_LEASE_RENEWED",
            tenant_id=task.tenant_id,
            payload={
                "qc_task_id": task.qc_task_id,
                "worker_id": worker_id,
                "lease_expires_at": expires.isoformat(),
                "attempt_count": task.attempt_count,
            },
            owner_action=QC_TASK_PROCESS_ACTION,
            conflict_detail="QC 租约续期并发冲突，请重试",
        )
        return task

    async def finish(
        self,
        *,
        task_id: str,
        worker_id: str,
        result: Literal["PASSED", "REJECTED"],
        qc_report_id: str,
        evidence_refs: list[str] | None = None,
    ) -> QCTask:
        task = await self._load(task_id)
        task.require_lease_owner(worker_id)
        target = QCTaskStatus(result)
        payload = {
            "qc_task_id": task.qc_task_id,
            "worker_id": worker_id,
            "qc_report_id": qc_report_id,
            "evidence_refs": list(evidence_refs or []),
            "attempt_count": task.attempt_count,
        }
        # 两个分支各写字面量：事件契约靠源码字面量双向核对。
        if target is QCTaskStatus.PASSED:
            await advance_status(
                task,
                target,
                repository=self._repository,
                outbox_writer=self._outbox_writer,
                aggregate_type="QCTask",
                event_type="QC_TASK_PASSED",
                tenant_id=task.tenant_id,
                payload=payload,
                resource_type="QCTask",
                owner_action=QC_TASK_PROCESS_ACTION,
                conflict_detail="QC 判定并发冲突，请重试",
                domain_operation=lambda audit_ref, action: task.finish(
                    worker_id, target, qc_report_id, audit_ref
                ),
            )
        else:
            await advance_status(
                task,
                target,
                repository=self._repository,
                outbox_writer=self._outbox_writer,
                aggregate_type="QCTask",
                event_type="QC_TASK_REJECTED",
                tenant_id=task.tenant_id,
                payload=payload,
                resource_type="QCTask",
                owner_action=QC_TASK_PROCESS_ACTION,
                conflict_detail="QC 判定并发冲突，请重试",
                domain_operation=lambda audit_ref, action: task.finish(
                    worker_id, target, qc_report_id, audit_ref
                ),
            )
        return task

    async def release_expired(self, *, task_id: str) -> QCTask:
        task = await self._load(task_id)
        await advance_status(
            task,
            QCTaskStatus.QUEUED,
            repository=self._repository,
            outbox_writer=self._outbox_writer,
            aggregate_type="QCTask",
            event_type="QC_TASK_LEASE_EXPIRED",
            tenant_id=task.tenant_id,
            payload={
                "qc_task_id": task.qc_task_id,
                "worker_id": task.lease_owner,
                "attempt_count": task.attempt_count,
            },
            resource_type="QCTask",
            owner_action=QC_TASK_PROCESS_ACTION,
            conflict_detail="QC 租约回收并发冲突，请重试",
            domain_operation=lambda audit_ref, action: task.release_expired(
                _now(), audit_ref
            ),
        )
        return task


__all__ = ["QCTaskService"]
