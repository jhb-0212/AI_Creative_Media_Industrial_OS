"""GenerationTask 聚合根（生成任务记录，R16-D3）。

对齐契约：
- contracts/schemas/object_schemas.schema.json#GenerationTask；
- contracts/state_machines/state_machines.yaml → objects → GenerationTask
  （REQUESTED → SUCCEEDED / FAILED，两条边归 GENERATION.REQUEST）。

MVP 为同步执行：GENERATION.REQUEST 校验预算/配额/依赖/约束快照通过后创建
REQUESTED，并同步推进 SUCCEEDED / FAILED。异步 RUNNING 状态与 Worker 领取/
租约/崩溃恢复归 Phase 2/3（无 MVP 动作拥有这些边），本批不建模。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.shared.entity import Entity
from services.project_planning.domain.enums import GenerationTaskStatus
from services.project_planning.domain.state_machine import GENERATION_TASK_LIFECYCLE

_OWNER_MODULE = "production-planning"


def _blank(value: str) -> bool:
    return value is None or not str(value).strip()


@dataclass
class GenerationTask(Entity):
    """生成任务记录（REQUESTED 出生，SUCCEEDED/FAILED 终态）。"""

    ENTITY_TYPE = "GenerationTask"

    owner_module: str = _OWNER_MODULE

    generation_task_id: str = ""
    project_id: str = ""
    shot_id: str = ""
    shot_revision_id: str = ""
    production_route_ref: str = ""
    model_adapter_ref: str = ""
    constraint_snapshot_id: str = ""
    budget_ref: str = ""
    prompt_ref: str = ""
    status: GenerationTaskStatus = GenerationTaskStatus.REQUESTED

    def __post_init__(self) -> None:
        super()._validate_envelope()
        self.generation_task_id = self.generation_task_id.strip() or self.entity_id
        for name in (
            "generation_task_id",
            "project_id",
            "shot_id",
            "shot_revision_id",
            "production_route_ref",
            "model_adapter_ref",
            "constraint_snapshot_id",
            "budget_ref",
            "prompt_ref",
        ):
            if _blank(getattr(self, name)):
                raise DomainError(
                    ErrorRegistry.VALIDATION_ERROR, detail=f"{name} 不能为空"
                )
        for name, max_len in (
            ("generation_task_id", 64),
            ("project_id", 64),
            ("shot_id", 64),
            ("shot_revision_id", 64),
            ("production_route_ref", 64),
            ("model_adapter_ref", 128),
            ("constraint_snapshot_id", 128),
            ("budget_ref", 128),
            ("prompt_ref", 200),
        ):
            if len(getattr(self, name)) > max_len:
                raise DomainError(
                    ErrorRegistry.VALIDATION_ERROR,
                    detail=f"{name} 长度超过上限 {max_len}",
                )

    def transition_to(
        self,
        target: GenerationTaskStatus,
        audit_ref: str,
        action: str,
    ) -> GenerationTask:
        """执行 REQUESTED → SUCCEEDED / FAILED，终态不可原地复活。"""
        GENERATION_TASK_LIFECYCLE.edge_owned_by(self.status, target, action)
        self.status = target
        self._advance_version(audit_ref)
        return self

    def to_dict(self) -> dict[str, Any]:
        data = self._envelope_dict()
        data.update(
            {
                "generation_task_id": self.generation_task_id,
                "project_id": self.project_id,
                "shot_id": self.shot_id,
                "shot_revision_id": self.shot_revision_id,
                "production_route_ref": self.production_route_ref,
                "model_adapter_ref": self.model_adapter_ref,
                "constraint_snapshot_id": self.constraint_snapshot_id,
                "budget_ref": self.budget_ref,
                "prompt_ref": self.prompt_ref,
            }
        )
        return data


__all__ = ["GenerationTask"]
