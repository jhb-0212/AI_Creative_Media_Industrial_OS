"""GenerationTask 应用服务（生成请求 + 异步租约，R16-D3 / Phase 2 §1）。

两种执行模式：
- 同步（MVP）：``result`` 显式给出，REQUESTED 与终态同事务；
- 异步（Phase 2）：``result=None``，任务停在 REQUESTED 等 Worker 持租约领取。

为什么租约操作全部走本服务而不是端点里直接改字段
--------------------------------------------------
每一次租约变更（领取 / 续租 / 终结 / 到期回收）都必须同时留下状态事实与事件。
散在端点里写，任何一处漏发事件都不会被闸门发现；走 ``advance_status`` /
``save_versioned_entity`` 这两条唯一入口，写事件不是「记得写」，而是结构上无法跳过。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast
from uuid import UUID, uuid4

from packages.errors.error_codes import DomainError, ErrorRegistry
from services.production_planning.domain.generation_task import GenerationTask
from services.project_planning.domain.enums import GenerationTaskStatus
from services.project_planning.domain.state_machine import (
    GENERATION_PROCESS_ACTION,
    GENERATION_REQUEST_ACTION,
)
from services.project_planning.service.transition import advance_status, save_versioned_entity


def _now() -> datetime:
    return datetime.now(UTC)


class GenerationService:
    def __init__(self, repository: Any, outbox_writer: Any) -> None:
        self._repository = repository
        self._outbox_writer = outbox_writer

    async def request_generation(
        self,
        *,
        project_id: str,
        shot_id: str,
        shot_revision_id: str,
        production_route_ref: str,
        model_adapter_ref: str,
        constraint_snapshot_id: str,
        budget_ref: str,
        prompt_ref: str,
        result: Literal["SUCCEEDED", "FAILED"] | None,
        created_by: str,
        tenant_id: UUID,
        generation_task_id: str = "",
    ) -> GenerationTask:
        task = GenerationTask(
            entity_id=str(uuid4()),
            created_by=created_by,
            tenant_id=tenant_id,
            generation_task_id=generation_task_id,
            project_id=project_id,
            shot_id=shot_id,
            shot_revision_id=shot_revision_id,
            production_route_ref=production_route_ref,
            model_adapter_ref=model_adapter_ref,
            constraint_snapshot_id=constraint_snapshot_id,
            budget_ref=budget_ref,
            prompt_ref=prompt_ref,
        )
        await self._repository.add(task)

        # 异步模式：创建即返回 REQUESTED，等 Worker 领取。同步模式保留 MVP 行为。
        if result is None:
            return task

        target = GenerationTaskStatus(result)
        await advance_status(
            task,
            target,
            repository=self._repository,
            outbox_writer=self._outbox_writer,
            aggregate_type="GenerationTask",
            event_type="GENERATION_REQUESTED",
            tenant_id=tenant_id,
            payload={
                "generation_task_id": task.generation_task_id,
                "project_id": task.project_id,
                "shot_id": task.shot_id,
                "shot_revision_id": task.shot_revision_id,
                "model_adapter_ref": task.model_adapter_ref,
                "created_by": created_by,
                "tenant_id": str(tenant_id),
            },
            resource_type="GenerationTask",
            owner_action=GENERATION_REQUEST_ACTION,
            conflict_detail="生成任务并发冲突，请基于最新状态重试",
            actor_field="created_by",
            actor=created_by,
        )
        return task

    # ---- Phase 2：异步租约（GENERATION.PROCESS）-------------------------- #

    async def _load(self, task_id: str) -> GenerationTask:
        task = await self._repository.get_by_id(task_id)
        if task is None:
            raise DomainError(
                ErrorRegistry.NOT_FOUND, detail=f"生成任务不存在: {task_id}"
            )
        return cast(GenerationTask, task)

    async def claim(
        self,
        *,
        task_id: str,
        worker_id: str,
        lease_seconds: int,
    ) -> GenerationTask:
        """REQUESTED → RUNNING：领取租约。"""
        task = await self._load(task_id)
        expires = _now() + timedelta(seconds=lease_seconds)
        await advance_status(
            task,
            GenerationTaskStatus.RUNNING,
            repository=self._repository,
            outbox_writer=self._outbox_writer,
            aggregate_type="GenerationTask",
            event_type="GENERATION_STARTED",
            tenant_id=task.tenant_id,
            payload={
                "generation_task_id": task.generation_task_id,
                "worker_id": worker_id,
                "lease_expires_at": expires.isoformat(),
                "attempt_count": task.attempt_count + 1,
            },
            resource_type="GenerationTask",
            owner_action=GENERATION_PROCESS_ACTION,
            conflict_detail="任务已被其他 Worker 领取，请勿重复领取",
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
    ) -> GenerationTask:
        """心跳续租：不迁移状态，只前移到期时间。"""
        task = await self._load(task_id)
        task.require_lease_owner(worker_id)
        expires = _now() + timedelta(seconds=lease_seconds)
        await save_versioned_entity(
            task,
            mutation=lambda audit_ref, action: task.renew(
                worker_id, expires, audit_ref
            ),
            audit_ref=f"lease-renew:{uuid4()}",
            repository=self._repository,
            outbox_writer=self._outbox_writer,
            aggregate_type="GenerationTask",
            aggregate_id=task.entity_id,
            event_type="GENERATION_LEASE_RENEWED",
            tenant_id=task.tenant_id,
            payload={
                "generation_task_id": task.generation_task_id,
                "worker_id": worker_id,
                "lease_expires_at": expires.isoformat(),
                "attempt_count": task.attempt_count,
            },
            owner_action=GENERATION_PROCESS_ACTION,
            conflict_detail="租约续期并发冲突，请重试",
        )
        return task

    async def finish(
        self,
        *,
        task_id: str,
        worker_id: str,
        result: Literal["SUCCEEDED", "FAILED"],
        evidence_ref: str = "",
        asset_version_id: str = "",
        quality_profile_version: str = "",
    ) -> GenerationTask:
        """RUNNING → SUCCEEDED / FAILED：只有租约持有者能终结。

        SUCCEEDED 可同时登记产物引用（同一次版本推进落库）。FAILED 不接受产物引用：
        失败任务不该出现在 QC 队列，也就不该留下「可被扫描到」的成功标记。
        """
        task = await self._load(task_id)
        task.require_lease_owner(worker_id)
        target = GenerationTaskStatus(result)
        if target is GenerationTaskStatus.SUCCEEDED and asset_version_id:
            task.attach_product_refs(asset_version_id, quality_profile_version)
        payload = {
            "generation_task_id": task.generation_task_id,
            "worker_id": worker_id,
            "evidence_ref": evidence_ref,
            "attempt_count": task.attempt_count,
        }
        # 两个分支各写一次字面量 event_type：事件契约靠源码字面量做双向核对，
        # 用三元表达式拼事件名会让「登记了却查不到发出点」变成误报。
        if target is GenerationTaskStatus.SUCCEEDED:
            await advance_status(
                task,
                target,
                repository=self._repository,
                outbox_writer=self._outbox_writer,
                aggregate_type="GenerationTask",
                event_type="GENERATION_SUCCEEDED",
                tenant_id=task.tenant_id,
                payload=payload,
                resource_type="GenerationTask",
                owner_action=GENERATION_PROCESS_ACTION,
                conflict_detail="任务终结并发冲突，请重试",
            )
        else:
            await advance_status(
                task,
                target,
                repository=self._repository,
                outbox_writer=self._outbox_writer,
                aggregate_type="GenerationTask",
                event_type="GENERATION_FAILED",
                tenant_id=task.tenant_id,
                payload=payload,
                resource_type="GenerationTask",
                owner_action=GENERATION_PROCESS_ACTION,
                conflict_detail="任务终结并发冲突，请重试",
            )
        return task

    async def release_expired(self, *, task_id: str) -> GenerationTask:
        """RUNNING → REQUESTED：租约到期回收，任务可被重新领取。

        由扫尾进程调用：判据是 ``lease_expires_at``，与谁持有无关。
        """
        task = await self._load(task_id)
        await advance_status(
            task,
            GenerationTaskStatus.REQUESTED,
            repository=self._repository,
            outbox_writer=self._outbox_writer,
            aggregate_type="GenerationTask",
            event_type="GENERATION_LEASE_EXPIRED",
            tenant_id=task.tenant_id,
            payload={
                "generation_task_id": task.generation_task_id,
                "worker_id": task.lease_owner,
                "attempt_count": task.attempt_count,
            },
            resource_type="GenerationTask",
            owner_action=GENERATION_PROCESS_ACTION,
            conflict_detail="租约回收并发冲突，请重试",
            domain_operation=lambda audit_ref, action: task.release_expired(
                _now(), audit_ref
            ),
        )
        return task


__all__ = ["GenerationService"]
