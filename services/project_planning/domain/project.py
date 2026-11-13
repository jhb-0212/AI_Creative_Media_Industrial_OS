"""Project 聚合根（领域模型，纯 Python，无框架依赖）。

对齐契约：
- 8.1 核心实体：Project 为项目层级聚合根（Project → Episode → Scene → Shot）
- 8.2 标识与版本规则：主键 UUID，project_id 不复用，时间存 UTC
- C.3.1 通用实体信封：entity_id/entity_type/schema_version/entity_version/status/
  owner_module/created_at/created_by/updated_at/audit_ref

设计要点（不屎山代码）：
- 领域模型为纯数据 + 行为，不依赖 ORM / FastAPI，保证可测试、可演进。
- 状态变更通过显式方法，未登记转移一律拒绝（31.5 规则精神）。
- 终态（ARCHIVED）不可原地复活（红线「终态不可原地复活」）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from services.project_planning.domain.enums import ProjectStatus, ProjectTier

# 契约 C.3.1 通用实体信封的 schema_version 固定值（V2.0.5）
SCHEMA_VERSION = 205
OWNER_MODULE = "project-planning"


def _utcnow() -> datetime:
    """统一 UTC 时间（8.2 规则：所有时间统一存 UTC）。"""
    return datetime.now(timezone.utc)


@dataclass
class Project:
    """项目聚合根。

    不可变核心字段用 frozen 语义约束；状态迁移返回新实例或显式变更，
    避免外部随意篡改。
    """

    entity_id: str
    project_name: str
    project_tier: ProjectTier
    created_by: str
    status: ProjectStatus = ProjectStatus.DRAFT
    # 统一信封字段
    entity_type: str = "Project"
    schema_version: int = SCHEMA_VERSION
    entity_version: int = 1
    owner_module: str = OWNER_MODULE
    created_at: datetime = field(default_factory=_utcnow)
    updated_at: datetime = field(default_factory=_utcnow)
    audit_ref: str | None = None

    # 允许的状态转移表（未登记转移一律拒绝）
    _ALLOWED_TRANSITIONS: dict[ProjectStatus, frozenset[ProjectStatus]] = field(
        default_factory=lambda: {
            ProjectStatus.DRAFT: frozenset({ProjectStatus.ACTIVE, ProjectStatus.ARCHIVED}),
            ProjectStatus.ACTIVE: frozenset({ProjectStatus.ON_HOLD, ProjectStatus.COMPLETED}),
            ProjectStatus.ON_HOLD: frozenset({ProjectStatus.ACTIVE, ProjectStatus.ARCHIVED}),
            ProjectStatus.COMPLETED: frozenset({ProjectStatus.ARCHIVED}),
            ProjectStatus.ARCHIVED: frozenset(),  # 终态不可原地复活
        },
        init=False,
        repr=False,
    )

    def transition_to(self, target: ProjectStatus, audit_ref: str) -> "Project":
        """执行状态迁移，未登记转移拒绝。"""
        allowed = self._ALLOWED_TRANSITIONS.get(self.status, frozenset())
        if target not in allowed:
            raise ValueError(
                f"非法状态转移：{self.status.value} -> {target.value}（未登记，已拒绝）"
            )
        self.status = target
        self.entity_version += 1
        self.updated_at = _utcnow()
        self.audit_ref = audit_ref
        return self

    def to_dict(self) -> dict:
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
            "project_name": self.project_name,
            "project_tier": self.project_tier.value,
        }
