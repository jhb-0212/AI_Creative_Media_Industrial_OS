"""ScriptRevision 聚合根（领域模型，纯 Python，无框架依赖）。

对齐契约：
- 31.6 Action Registry → SCRIPT.IMPORT：resource_type=ScriptRevision，
  scope=project/episode，MEDIUM 风险，Token=Y，导演/编剧/制片，
  检查项目写权限、文件安全与导入来源 → SCRIPT_IMPORTED。
- ADR-032：AI 剧本拆镜输出默认草案，人工确认前不得写入 ScriptLock。
  剧本导入产出 DRAFT 状态的可编辑版本，锁定是独立动作（SCRIPT.LOCK.APPROVE）。
- C.3.1 通用实体信封。

设计要点（不屎山代码）：
- 领域模型纯数据 + 行为，不依赖 ORM / FastAPI。
- 状态变更经显式方法，未登记转移一律拒绝（31.5）。
- 终态（SUPERSEDED）不可原地复活（红线）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from services.project_planning.domain.enums import ScriptStatus

# 契约 C.3.1 通用实体信封的 schema_version 固定值（V2.0.5）
SCHEMA_VERSION = 205
OWNER_MODULE = "project-planning"


def _utcnow() -> datetime:
    """统一 UTC 时间（8.2 规则：所有时间统一存 UTC）。"""
    return datetime.now(timezone.utc)


# ScriptRevision 状态转移表（对齐 state_machines.yaml → ScriptRevision）。
# DRAFT→LOCKED；DRAFT/LOCKED→SUPERSEDED；SUPERSEDED 为终态。
# 公开导出，供契约一致性校验脚本反射比对（与 state_machine.py 命名一致）。
ALLOWED_TRANSITIONS: dict[ScriptStatus, frozenset[ScriptStatus]] = {
    ScriptStatus.DRAFT: frozenset({ScriptStatus.LOCKED, ScriptStatus.SUPERSEDED}),
    ScriptStatus.LOCKED: frozenset({ScriptStatus.SUPERSEDED}),
    ScriptStatus.SUPERSEDED: frozenset(),  # 终态不可原地复活
}


@dataclass
class ScriptRevision:
    """剧本版本聚合根。

    一次导入产生一个不可变版本；新版本导入使旧版本转 SUPERSEDED。
    """

    entity_id: str
    project_id: str
    episode_ref: str
    title: str
    created_by: str
    status: ScriptStatus = ScriptStatus.DRAFT
    # 统一信封字段
    entity_type: str = "ScriptRevision"
    schema_version: int = SCHEMA_VERSION
    entity_version: int = 1
    owner_module: str = OWNER_MODULE
    created_at: datetime = field(default_factory=_utcnow)
    updated_at: datetime = field(default_factory=_utcnow)
    audit_ref: str | None = None

    def transition_to(self, target: ScriptStatus, audit_ref: str) -> "ScriptRevision":
        """执行状态迁移，未登记转移拒绝（31.5）。"""
        allowed = ALLOWED_TRANSITIONS.get(self.status, frozenset())
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
            "project_id": self.project_id,
            "episode_ref": self.episode_ref,
            "title": self.title,
        }
