"""GenerationTask 应用服务（生成请求，R16-D3）。

MVP 同步执行：GENERATION.REQUEST 校验预算/配额/依赖/约束快照通过后创建
REQUESTED，并同步推进 SUCCEEDED / FAILED（由请求方声明的 result 决定）。
异步 RUNNING 与 Worker 生命周期归 Phase 2/3。
"""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID, uuid4

from services.production_planning.domain.generation_task import GenerationTask
from services.project_planning.domain.enums import GenerationTaskStatus
from services.project_planning.domain.state_machine import GENERATION_REQUEST_ACTION
from services.project_planning.service.transition import advance_status


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
        result: Literal["SUCCEEDED", "FAILED"],
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


__all__ = ["GenerationService"]
