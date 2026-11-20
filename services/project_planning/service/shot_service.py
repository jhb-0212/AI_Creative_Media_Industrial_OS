"""Shot 应用服务（镜头状态推进与读取）。

创建不在本服务：Shot 的唯一诞生点是 ``SHOT.REVISION.CONFIRM``（ADR-R18 §12），
由 ``ShotDraftService.confirm_draft`` 在同一事务内原子创建 Shot + 首个 ShotRevision。
若这里再开一个 ``create_shot``，就会出现「绕过确认直接造镜头」的第二条路径。

守卫所需字段由本服务写入实体，再由领域层 ``transition_to`` 判定——
赋值与判定分离是刻意的：守卫必须能读到「这次请求打算写什么」，
但「能不能写」仍由领域层说了算。
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.shared.outbox_writer import OutboxWriter
from services.project_planning.domain.enums import ReworkTaskStatus, ShotStatus
from services.project_planning.domain.shot import Shot
from services.project_planning.domain.state_machine import SHOT_STATUS_CHANGE_ACTION
from services.project_planning.repository.shot_planning_repositories import (
    ReworkTaskRepository,
    ShotRepository,
)
from services.project_planning.service.transition import advance_status


class ShotService:
    def __init__(
        self,
        repository: ShotRepository,
        rework_task_repository: ReworkTaskRepository,
        outbox_writer: OutboxWriter,
    ) -> None:
        self._repository = repository
        self._rework_task_repository = rework_task_repository
        self._outbox_writer = outbox_writer

    async def get_shot(self, entity_id: str) -> Shot:
        shot = await self._repository.get_by_id(entity_id)
        if shot is None:
            raise DomainError(ErrorRegistry.NOT_FOUND, detail=f"镜头不存在: {entity_id}")
        return shot

    async def change_status(
        self,
        *,
        entity_id: str,
        target_status: ShotStatus,
        changed_by: str,
        tenant_id: UUID,
        blocked_reason: str | None = None,
        rework_basis_ref: str | None = None,
        cancellation_ref: str | None = None,
        cancelled_at: datetime | None = None,
    ) -> Shot:
        changed_by = changed_by.strip()
        if not changed_by:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR, detail="changed_by 不能为空"
            )
        shot = await self._repository.get_by_id(entity_id)
        if shot is None:
            raise DomainError(ErrorRegistry.NOT_FOUND, detail=f"镜头不存在: {entity_id}")

        # 返工依据必须在库层可验证：引用一个不存在的 ReworkTask，等于用字符串
        # 绕过验收。这一步在赋值前做，避免把实体改成半合法状态再抛错。
        if rework_basis_ref is not None:
            task = await self._rework_task_repository.get_by_id(rework_basis_ref)
            if task is None:
                raise DomainError(
                    ErrorRegistry.NOT_FOUND,
                    detail=f"返工依据不存在: {rework_basis_ref}",
                )
            if task.status is ReworkTaskStatus.CLOSED:
                raise DomainError(
                    ErrorRegistry.STATE_TRANSITION_DENIED,
                    detail="已关闭的返工任务不能作为新的返工依据",
                )

        shot.blocked_reason = blocked_reason
        shot.rework_basis_ref = rework_basis_ref
        shot.cancellation_ref = cancellation_ref
        shot.cancelled_at = cancelled_at

        # 取消与返工回退的授权依据写进迁移事实（D2：authority_ref 承载，
        # 不新建 ApprovalRecord 对象）。
        authority_ref = cancellation_ref or rework_basis_ref

        await advance_status(
            shot,
            target_status,
            repository=self._repository,
            outbox_writer=self._outbox_writer,
            aggregate_type="Shot",
            event_type="SHOT_STATUS_CHANGED",
            tenant_id=tenant_id,
            payload={
                "shot_id": shot.entity_id,
                "project_id": shot.project_id,
                "scene_id": shot.scene_id,
            },
            resource_type="Shot",
            owner_action=SHOT_STATUS_CHANGE_ACTION,
            conflict_detail="镜头状态并发冲突，请基于最新状态重试",
            actor_field="changed_by",
            actor=changed_by,
            authority_ref=authority_ref,
        )
        return shot
