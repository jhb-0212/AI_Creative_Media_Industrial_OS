"""Project 聚合根（领域模型，纯 Python，无框架依赖）。

对齐契约：
- 8.1 核心实体：Project 为项目层级聚合根（Project → Episode → Scene → Shot）
- 8.2 标识与版本规则：主键 UUID，project_id 不复用，时间存 UTC
- C.3.1 通用实体信封（收敛到 domain/entity.py 的 Entity 基类）

设计要点（不屎山代码）：
- 领域模型为纯数据 + 行为，不依赖 ORM / FastAPI。
- 状态变更通过显式方法，未登记转移一律拒绝（31.5）。
- 终态（ARCHIVED）不可原地复活（红线「终态不可原地复活」）。
- 信封公共字段/校验/序列化继承自 Entity，本类只保留业务字段与行为。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from services.project_planning.domain.entity import Entity
from services.project_planning.domain.enums import ProjectStatus, ProjectTier
from services.project_planning.domain.state_machine import assert_transition


@dataclass
class Project(Entity):
    """项目聚合根。

    状态只能沿状态机向前迁移，终态（ARCHIVED）不可原地复活。
    """

    ENTITY_TYPE = "Project"

    # 业务字段（信封字段由 Entity 基类提供）。
    project_name: str = ""
    project_tier: ProjectTier = ProjectTier.B
    status: ProjectStatus = ProjectStatus.DRAFT

    def __post_init__(self) -> None:
        """防御性校验：信封公共字段 + 业务字段（对齐 ORM 列定义）。"""
        super()._validate_envelope()
        if not self.project_name.strip():
            raise ValueError("project_name 不能为空")
        if len(self.project_name) > 200:
            raise ValueError("project_name 长度超过上限 200")

    def transition_to(self, target: ProjectStatus, audit_ref: str) -> Project:
        """执行状态迁移，未登记转移拒绝（31.5）。

        转移合法性由 domain/state_machine.py 的单一权威转移表校验。
        """
        assert_transition(self.status, target)
        self.status = target
        self._advance_version(audit_ref)
        return self

    def to_dict(self) -> dict[str, Any]:
        """序列化为契约信封结构。"""
        data = self._envelope_dict()
        data.update(
            {
                "project_name": self.project_name,
                "project_tier": self.project_tier.value,
            }
        )
        return data
