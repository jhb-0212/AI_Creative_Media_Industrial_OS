"""Episode 聚合根（领域模型，纯 Python，无框架依赖）。

对齐契约：
- 8.1 核心实体：Project → Episode → Scene → Shot 的项目层级聚合根，
  Episode 归属于 Project（一对多）。
- contracts/state_machines/state_machines.yaml → objects → Episode：
  DRAFT→ACTIVE→ARCHIVED。
- C.3.1 通用实体信封（收敛到 domain/entity.py 的 Entity 基类）。

设计要点（不屎山代码）：
- 领域模型纯数据 + 行为，不依赖 ORM / FastAPI。
- 状态变更经显式方法，未登记转移一律拒绝（31.5）。
- 终态（ARCHIVED）不可原地复活（红线）。
- 信封公共字段/校验/序列化继承自 Entity。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from packages.errors.error_codes import DomainError, ErrorRegistry
from services.project_planning.domain.entity import OWNER_MODULE, Entity
from services.project_planning.domain.enums import EpisodeStatus
from services.project_planning.domain.state_machine import assert_transition_episode


@dataclass
class Episode(Entity):
    """集数聚合根（归属于 Project）。

    状态只能沿状态机向前迁移，终态（ARCHIVED）不可原地复活。
    """

    ENTITY_TYPE = "Episode"

    # 数据所有权：信封 owner_module 覆写为本模块（基类默认 core）。
    owner_module: str = OWNER_MODULE

    # 业务字段（信封字段由 Entity 基类提供）。
    project_id: str = ""
    episode_ref: str = ""
    title: str = ""
    status: EpisodeStatus = EpisodeStatus.DRAFT

    def __post_init__(self) -> None:
        """防御性校验：信封公共字段 + 业务字段（对齐 ORM 列定义）。"""
        super()._validate_envelope()
        for name, max_len in (
            ("project_id", 64),
            ("episode_ref", 64),
            ("title", 200),
        ):
            value = getattr(self, name)
            if not value or not value.strip():
                raise DomainError(
                    ErrorRegistry.VALIDATION_ERROR, detail=f"{name} 不能为空"
                )
            if len(value) > max_len:
                raise DomainError(
                    ErrorRegistry.VALIDATION_ERROR,
                    detail=f"{name} 长度超过上限 {max_len}",
                )

    def transition_to(self, target: EpisodeStatus, audit_ref: str) -> Episode:
        """执行状态迁移，未登记转移拒绝（31.5）。"""
        assert_transition_episode(self.status, target)
        self.status = target
        self._advance_version(audit_ref)
        return self

    def to_dict(self) -> dict[str, Any]:
        """序列化为契约信封结构。"""
        data = self._envelope_dict()
        data.update(
            {
                "project_id": self.project_id,
                "episode_ref": self.episode_ref,
                "title": self.title,
            }
        )
        return data
