"""GenerationTask 聚合根（生成任务，领域模型，纯 Python）。

对齐契约：
- object_schemas.schema.json → GenerationTask（V205-R14-A6 冻结）；
- 动作矩阵 GENERATION.REQUEST 行：准入由"预算、配额、依赖与约束快照"
  校验，审计事件 GENERATION_REQUESTED，负向 ACT-GEN-NEG-001；
- A6 冻结 description：**失败为终结态**——重试以新任务承载（attempt+1），
  不在原任务上复活（9.4 / 终态不可原地复活红线）；
- 基线 31.3：生成有效 ConstraintPack（如适用；无锁定约束镜头标记
  NOT_APPLICABLE）——constraint_snapshot_ref 接受 NOT_APPLICABLE 哨兵。

实现扩展列（additionalProperties 允许，不出现在契约 required）：
- retry_of：被重试的失败任务 id（可空）。attempt 链的可追溯锚点——
  重试不是原任务复活，是引用终态失败任务的新任务。

边界硬约束：
- 准入校验（引用存在、归属项目、路线 VERIFIED）由 GenerationService 在
  受理入口判定；数据库层守卫保证状态只能沿登记边推进。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from packages.errors.error_codes import DomainError, ErrorRegistry
from services.generation_execution.domain.entity import OWNER_MODULE, Entity
from services.generation_execution.domain.enums import GenerationTaskStatus

#: 无锁定约束镜头的约束快照哨兵（基线 31.3）。
CONSTRAINT_NOT_APPLICABLE = "NOT_APPLICABLE"


@dataclass
class GenerationTask(Entity):
    """生成任务聚合根（预算/配额/依赖/约束快照的准入校验载体）。"""

    ENTITY_TYPE = "GenerationTask"

    # 数据所有权：信封 owner_module 覆写为本模块。
    owner_module: str = OWNER_MODULE

    # 业务字段（信封字段由 Entity 基类提供；status 是状态机列）。
    project_id: str = ""
    shot_id: str = ""
    route_id: str = ""
    shot_spec_ref: str = ""
    visual_plan_ref: str = ""
    constraint_snapshot_ref: str = ""
    budget_reservation_ref: str = ""
    quota_reservation_ref: str = ""
    dependency_refs: list[str] = field(default_factory=list)
    attempt: int = 1
    retry_of: str | None = None
    status: GenerationTaskStatus = GenerationTaskStatus.PENDING
    # 执行产出引用（worker 写入；实现扩展列，不出契约 required）。
    result_ref: str | None = None

    def __post_init__(self) -> None:
        """防御性校验：信封公共字段 + 业务字段（对齐 ORM 列定义）。"""
        super()._validate_envelope()
        for name, value in (
            ("project_id", self.project_id),
            ("shot_id", self.shot_id),
            ("route_id", self.route_id),
            ("shot_spec_ref", self.shot_spec_ref),
            ("visual_plan_ref", self.visual_plan_ref),
            ("constraint_snapshot_ref", self.constraint_snapshot_ref),
            ("budget_reservation_ref", self.budget_reservation_ref),
            ("quota_reservation_ref", self.quota_reservation_ref),
        ):
            if not value.strip():
                raise DomainError(
                    ErrorRegistry.VALIDATION_ERROR, detail=f"{name} 不能为空"
                )
        if self.attempt < 1:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR, detail="attempt 必须 >= 1"
            )
        if self.attempt > 1 and not self.retry_of:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail="attempt > 1 必须携带 retry_of（重试以新任务承载，9.4）",
            )
        if self.attempt == 1 and self.retry_of:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail="首建任务（attempt=1）不得携带 retry_of",
            )

    def to_dict(self) -> dict[str, Any]:
        """序列化为契约信封结构（generation_task_id 是主标识字段）。"""
        data = self._envelope_dict()
        data.update(
            {
                "generation_task_id": self.entity_id,
                "project_id": self.project_id,
                "shot_id": self.shot_id,
                "route_id": self.route_id,
                "shot_spec_ref": self.shot_spec_ref,
                "visual_plan_ref": self.visual_plan_ref,
                "constraint_snapshot_ref": self.constraint_snapshot_ref,
                "budget_reservation_ref": self.budget_reservation_ref,
                "quota_reservation_ref": self.quota_reservation_ref,
                "dependency_refs": self.dependency_refs,
                "attempt": self.attempt,
                "retry_of": self.retry_of,
            }
        )
        return data
