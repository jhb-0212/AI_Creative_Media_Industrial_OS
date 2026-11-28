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
from uuid import UUID

from packages.errors.error_codes import DomainError, ErrorRegistry
from services.project_planning.domain.entity import OWNER_MODULE, Entity
from services.project_planning.domain.enums import ProjectStatus, ProjectTier
from services.project_planning.domain.state_machine import PROJECT_LIFECYCLE


@dataclass
class Project(Entity):
    """项目聚合根。

    状态只能沿状态机向前迁移，终态（ARCHIVED）不可原地复活。

    归属（F04）：organization_id 是组织归属键（可空=个人租户），与 tenant_id
    同构——是内部隔离/归属键，不进对外响应契约（见 to_dict 注释）。
    """

    ENTITY_TYPE = "Project"

    # 数据所有权：信封 owner_module 覆写为本模块（基类默认 core）。
    owner_module: str = OWNER_MODULE

    # 业务字段（信封字段由 Entity 基类提供）。
    project_name: str = ""
    project_tier: ProjectTier = ProjectTier.B
    status: ProjectStatus = ProjectStatus.DRAFT
    # 归属键：组织归属（可空 = 个人租户，无组织）。
    organization_id: UUID | None = None

    def __post_init__(self) -> None:
        """防御性校验：信封公共字段 + 业务字段（对齐 ORM 列定义）。"""
        super()._validate_envelope()
        if not self.project_name.strip():
            raise DomainError(ErrorRegistry.VALIDATION_ERROR, detail="project_name 不能为空")
        if len(self.project_name) > 200:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail="project_name 长度超过上限 200",
            )

    def transition_to(self, target: ProjectStatus, audit_ref: str, action: str) -> Project:
        """执行状态迁移：结构合法性 + **边所有权**双重校验（31.5 / N-07）。

        ``action`` 不是记录用的标签，是准入凭据：由 ``PROJECT_LIFECYCLE``
        判定这条边是否归该动作所有。不属于该动作的边，对本调用方不可达——
        这条规则在这里执行一次，而不是由每个调用方自行「记得先查」。

        唯一推进入口 ``advance_status`` 传入的正是它随后写进迁移事实的那个
        ``owner_action``：一个值，两处用途，不存在「事实记的动作」与「准入判的
        动作」不一致的可能。
        """
        PROJECT_LIFECYCLE.edge_owned_by(self.status, target, action)
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
