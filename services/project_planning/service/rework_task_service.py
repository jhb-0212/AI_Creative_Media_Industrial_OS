"""ReworkTask 应用服务（返工任务登记与状态推进）。

职责分离在领域层（``ReworkTask.assert_reviewer_not_owner``），本层只负责把
可信主体传进去。为什么不在领域 ``transition_to`` 里做：它需要「当前操作者」，
而唯一推进入口 ``advance_status`` 的契约只传 (target, audit_ref, action)。
把 actor 塞进 transition_to 会让那个参数变成「有时是准入凭据、有时是审批人」
的双重语义——审计表里最不该有的就是含糊。
"""

from __future__ import annotations

import uuid
from uuid import UUID

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.shared.outbox_writer import OutboxWriter
from packages.shared.port_errors import DuplicateEntityError, ReferentialIntegrityError
from services.project_planning.domain.enums import ReworkTaskStatus
from services.project_planning.domain.rework_task import ReworkTask
from services.project_planning.domain.state_machine import (
    REWORK_TASK_STATUS_CHANGE_ACTION,
)
from services.project_planning.repository.project_repo import ProjectRepository
from services.project_planning.repository.shot_planning_repositories import (
    ReworkTaskRepository,
)
from services.project_planning.service.transition import advance_status


class ReworkTaskService:
    def __init__(
        self,
        repository: ReworkTaskRepository,
        project_repository: ProjectRepository,
        outbox_writer: OutboxWriter,
    ) -> None:
        self._repository = repository
        self._project_repository = project_repository
        self._outbox_writer = outbox_writer

    async def create_task(
        self,
        *,
        project_id: str,
        issue_id: str,
        target_ref: str,
        defect_category: str,
        severity: str,
        root_cause: str,
        owner: str,
        return_node: str,
        affected_asset_version_ids: list[str],
        downstream_task_ids: list[str],
        created_by: str,
        tenant_id: UUID,
        constraint_pack_ref: str | None = None,
        entity_id: str | None = None,
    ) -> ReworkTask:
        stripped = {
            "project_id": project_id.strip(),
            "issue_id": issue_id.strip(),
            "target_ref": target_ref.strip(),
            "defect_category": defect_category.strip(),
            "severity": severity.strip(),
            "root_cause": root_cause.strip(),
            "owner": owner.strip(),
            "return_node": return_node.strip(),
            "created_by": created_by.strip(),
        }
        for name, value in stripped.items():
            if not value:
                raise DomainError(
                    ErrorRegistry.VALIDATION_ERROR, detail=f"{name} 不能为空"
                )

        project = await self._project_repository.get_by_id(stripped["project_id"])
        if project is None:
            raise DomainError(
                ErrorRegistry.NOT_FOUND,
                detail=f"项目不存在: {stripped['project_id']}",
            )

        if entity_id is not None:
            existing = await self._repository.get_by_id(entity_id)
            if existing is not None:
                _assert_same_request(existing, stripped)
                return existing

        task = ReworkTask(
            entity_id=entity_id or uuid.uuid4().hex,
            project_id=stripped["project_id"],
            issue_id=stripped["issue_id"],
            target_ref=stripped["target_ref"],
            defect_category=stripped["defect_category"],
            severity=stripped["severity"],
            root_cause=stripped["root_cause"],
            owner=stripped["owner"],
            return_node=stripped["return_node"],
            affected_asset_version_ids=list(affected_asset_version_ids),
            downstream_task_ids=list(downstream_task_ids),
            constraint_pack_ref=constraint_pack_ref,
            created_by=stripped["created_by"],
            tenant_id=tenant_id,
        )

        try:
            await self._repository.add(task)
        except ReferentialIntegrityError as exc:
            raise DomainError(
                ErrorRegistry.CONCURRENT_MODIFICATION,
                detail="返工任务归属的项目已变化，请基于最新数据重试",
            ) from exc
        except DuplicateEntityError as exc:
            existing = (
                await self._repository.get_by_id(entity_id)
                if entity_id is not None
                else None
            )
            if existing is not None:
                _assert_same_request(existing, stripped)
                return existing
            raise DomainError(
                ErrorRegistry.INTERNAL_ERROR, detail="返工任务写入冲突"
            ) from exc

        self._outbox_writer.record(
            aggregate_type="ReworkTask",
            aggregate_id=task.entity_id,
            event_type="REWORK_TASK_CREATED",
            tenant_id=tenant_id,
            payload={
                "rework_task_id": task.entity_id,
                "project_id": task.project_id,
                "target_ref": task.target_ref,
                "owner": task.owner,
                "severity": task.severity,
                "created_by": task.created_by,
                "tenant_id": str(tenant_id),
                "status": task.status.value,
            },
        )
        return task

    async def get_task(self, entity_id: str) -> ReworkTask:
        task = await self._repository.get_by_id(entity_id)
        if task is None:
            raise DomainError(
                ErrorRegistry.NOT_FOUND, detail=f"返工任务不存在: {entity_id}"
            )
        return task

    async def change_status(
        self,
        *,
        entity_id: str,
        target_status: ReworkTaskStatus,
        changed_by: str,
        tenant_id: UUID,
    ) -> ReworkTask:
        changed_by = changed_by.strip()
        if not changed_by:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR, detail="changed_by 不能为空"
            )
        task = await self._repository.get_by_id(entity_id)
        if task is None:
            raise DomainError(
                ErrorRegistry.NOT_FOUND, detail=f"返工任务不存在: {entity_id}"
            )

        # 职责分离只在关闭这一步要求：VERIFYING→CLOSED 必须有复核人，
        # 且复核人不得是返工执行人本人（7.6）。其它转移由动作权限约束。
        if target_status is ReworkTaskStatus.CLOSED:
            task.assert_reviewer_not_owner(changed_by)

        await advance_status(
            task,
            target_status,
            repository=self._repository,
            outbox_writer=self._outbox_writer,
            aggregate_type="ReworkTask",
            event_type="REWORK_TASK_STATUS_CHANGED",
            tenant_id=tenant_id,
            payload={
                "rework_task_id": task.entity_id,
                "project_id": task.project_id,
            },
            resource_type="ReworkTask",
            owner_action=REWORK_TASK_STATUS_CHANGE_ACTION,
            conflict_detail="返工任务状态并发冲突，请基于最新状态重试",
            actor_field="changed_by",
            actor=changed_by,
            authority_ref=changed_by if target_status is ReworkTaskStatus.CLOSED else None,
        )
        return task


def _assert_same_request(existing: ReworkTask, values: dict[str, str]) -> None:
    if (
        existing.project_id != values["project_id"]
        or existing.issue_id != values["issue_id"]
        or existing.target_ref != values["target_ref"]
        or existing.owner != values["owner"]
    ):
        raise DomainError(
            ErrorRegistry.IDEMPOTENCY_CONFLICT,
            detail="同一 entity_id 已存在且内容不同：幂等键不能复用于另一个请求",
        )
