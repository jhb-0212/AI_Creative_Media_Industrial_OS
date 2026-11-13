"""Episode 聚合根（领域模型，纯 Python，无框架依赖）。

对齐契约：
- 8.1 核心实体：Project → Episode → Scene → Shot 的项目层级聚合根，
  Episode 归属于 Project（一对多）。
- contracts/state_machines/state_machines.yaml → objects → Episode：
  DRAFT→ACTIVE→ARCHIVED。
- C.3.1 通用实体信封。

设计要点（不屎山代码）：
- 领域模型纯数据 + 行为，不依赖 ORM / FastAPI。
- 状态变更经显式方法，未登记转移一律拒绝（31.5）。
- 终态（ARCHIVED）不可原地复活（红线）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from services.project_planning.domain.enums import EpisodeStatus
from services.project_planning.domain.state_machine import assert_transition_episode

# 契约 C.3.1 通用实体信封的 schema_version 固定值（V2.0.5）
SCHEMA_VERSION = 205
OWNER_MODULE = "project-planning"


def _utcnow() -> datetime:
    """统一 UTC 时间（8.2 规则：所有时间统一存 UTC）。"""
    return datetime.now(UTC)


@dataclass
class Episode:
    """集数聚合根（归属于 Project）。

    「不可变」指版本/状态语义：状态只能沿状态机向前迁移，终态（ARCHIVED）
    不可原地复活。因 `transition_to` 需原地更新 status/entity_version，
    本类不声明 `@dataclass(frozen=True)`。
    """

    entity_id: str
    project_id: str
    episode_ref: str
    title: str
    created_by: str
    status: EpisodeStatus = EpisodeStatus.DRAFT
    # 统一信封字段
    entity_type: str = "Episode"
    schema_version: int = SCHEMA_VERSION
    entity_version: int = 1
    owner_module: str = OWNER_MODULE
    created_at: datetime = field(default_factory=_utcnow)
    updated_at: datetime = field(default_factory=_utcnow)
    audit_ref: str | None = None

    def __post_init__(self) -> None:
        """防御性校验：必填字段非空、长度不超限（对齐 ORM 列定义）。"""
        required: tuple[tuple[str, int], ...] = (
            ("entity_id", 64),
            ("project_id", 64),
            ("episode_ref", 64),
            ("title", 200),
            ("created_by", 64),
        )
        for field_name, max_len in required:
            value = getattr(self, field_name)
            if value is None or (isinstance(value, str) and not value.strip()):
                raise ValueError(f"{field_name} 不能为空")
            if len(value) > max_len:
                raise ValueError(f"{field_name} 长度超过上限 {max_len}")

    def transition_to(self, target: EpisodeStatus, audit_ref: str) -> Episode:
        """执行状态迁移，未登记转移拒绝（31.5）。

        转移合法性由 domain/state_machine.py 的 EPISODE_ALLOWED_TRANSITIONS
        单一权威转移表校验（单一事实源）。
        """
        assert_transition_episode(self.status, target)
        self.status = target
        self.entity_version += 1
        self.updated_at = _utcnow()
        self.audit_ref = audit_ref
        return self

    def to_dict(self) -> dict[str, Any]:
        """序列化为契约信封结构。"""
        return {
            "entity_id": self.entity_id,
            "entity_type": self.entity_type,
            "schema_version": self.schema_version,
            "entity_version": self.entity_version,
            "status": self.status.value,
            "owner_module": self.owner_module,
            "created_at": self.created_at.isoformat(),
            "created_by": self.created_by,
            "updated_at": self.updated_at.isoformat(),
            "audit_ref": self.audit_ref,
            "project_id": self.project_id,
            "episode_ref": self.episode_ref,
            "title": self.title,
        }
