"""Shot ORM 模型（映射到独立 schema：shot_planning）。

数据所有权（ADR-R27）：
- shot_planning 模块拥有 ShotDraft / ShotRevision / ShotSpec /
  ShotVisualPlan / ConstraintPack 表的写入权。
- 使用独立 schema「shot_planning」，与 project_planning 彻底隔离。
- 表结构严格对齐 C.3.1 通用实体信封字段（信封列收敛到 EntityMixin）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from packages.shared.database import Base
from packages.shared.orm import EntityMixin

# 独立 schema 名称（数据所有权边界）。
SCHEMA_NAME = "shot_planning"


class ShotDraftModel(EntityMixin, Base):
    """shot_drafts 表模型（AI 拆镜草案）。"""

    __tablename__ = "shot_drafts"
    __table_args__ = {"schema": SCHEMA_NAME}

    # 业务字段
    project_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    episode_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    scene_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    shot_code: Mapped[str] = mapped_column(String(64), nullable=False)
    intent_summary: Mapped[str] = mapped_column(Text, nullable=False)
    shot_description: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)


class ShotRevisionModel(EntityMixin, Base):
    """shot_revisions 表模型（正式镜头内容修订，append-only）。"""

    __tablename__ = "shot_revisions"

    # 业务字段
    shot_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    episode_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    scene_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    intent_summary: Mapped[str] = mapped_column(Text, nullable=False)
    shot_description: Mapped[str] = mapped_column(Text, nullable=False)
    revision: Mapped[int] = mapped_column(nullable=False)

    # 同 shot 内 revision 唯一（版本递增不覆盖）。
    __table_args__ = (
        Index(
            "uq_shot_planning_shot_revisions_shot_revision",
            "shot_id",
            "revision",
            unique=True,
        ),
        {"schema": SCHEMA_NAME},
    )


class ShotSpecModel(EntityMixin, Base):
    """shot_specs 表模型（镜头结构化规格，绑定 ShotRevision，8.12）。"""

    __tablename__ = "shot_specs"
    __table_args__ = {"schema": SCHEMA_NAME}

    # 业务字段
    shot_revision_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    shot_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    duration_ms: Mapped[int] = mapped_column(nullable=False)
    aspect_ratio: Mapped[str] = mapped_column(String(32), nullable=False)
    fps: Mapped[str] = mapped_column(String(32), nullable=False)
    coordinate_system: Mapped[str] = mapped_column(Text, nullable=False)
    camera_path: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    subject_paths: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    shot_size_changes: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    cut_points_frames: Mapped[list[int]] = mapped_column(JSONB, nullable=False)
    previz_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    control_signal_refs: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    locked_dimensions: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    # 锁定元数据（19.2：锁定人/时间/理由；审计引用走信封 audit_ref 列）。
    locked_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    locked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    lock_rationale: Mapped[str | None] = mapped_column(Text, nullable=True)


class ShotVisualPlanModel(EntityMixin, Base):
    """shot_visual_plans 表模型（镜头视觉方案，19.2 锁定/开放字段分离）。"""

    __tablename__ = "shot_visual_plans"
    __table_args__ = {"schema": SCHEMA_NAME}

    # 业务字段
    shot_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    intent_summary: Mapped[str] = mapped_column(Text, nullable=False)
    camera_language: Mapped[str] = mapped_column(String(128), nullable=False)
    shot_size: Mapped[str] = mapped_column(String(32), nullable=False)
    lens_range: Mapped[str] = mapped_column(String(64), nullable=False)
    motion_intent: Mapped[str] = mapped_column(Text, nullable=False)
    lighting_intent: Mapped[str] = mapped_column(Text, nullable=False)
    locked_fields: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    open_fields: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    revision: Mapped[int] = mapped_column(nullable=False)


class ConstraintPackModel(EntityMixin, Base):
    """constraint_packs 表模型（隐式连续性依赖约束包，7.9）。"""

    __tablename__ = "constraint_packs"
    __table_args__ = {"schema": SCHEMA_NAME}

    # 业务字段
    project_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    scene_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    shot_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    shot_group_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    applicability: Mapped[str] = mapped_column(String(16), nullable=False)
    locked_dimensions: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    constraints: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    source_previz_revision: Mapped[str | None] = mapped_column(String(64), nullable=True)
    version: Mapped[int] = mapped_column(nullable=False)
    approved_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class ShotVisualPlanStatusTransitionModel(Base):
    """`shot_visual_plan_status_transitions` 表模型（只读参考数据，ADR-R20）。

    这张表是 ShotVisualPlan 状态机在数据库层的表达：触发器守卫按它判定
    `status` 变化是否合法。不属于领域聚合，业务代码不可写入——应用角色
    只有 SELECT，写入只发生在迁移里。声明成模型让 alembic check 能校验
    这张表的结构。
    """

    __tablename__ = "shot_visual_plan_status_transitions"
    __table_args__ = {"schema": SCHEMA_NAME}

    from_status: Mapped[str] = mapped_column(String(32), primary_key=True)
    to_status: Mapped[str] = mapped_column(String(32), primary_key=True)


class ConstraintPackStatusTransitionModel(Base):
    """`constraint_pack_status_transitions` 表模型（只读参考数据，ADR-R20）。

    与 ShotVisualPlanStatusTransitionModel 同口径：ConstraintPack（7.9 权威
    状态机）在数据库层的表达。应用角色只读，写入只发生在迁移里。
    """

    __tablename__ = "constraint_pack_status_transitions"
    __table_args__ = {"schema": SCHEMA_NAME}

    from_status: Mapped[str] = mapped_column(String(32), primary_key=True)
    to_status: Mapped[str] = mapped_column(String(32), primary_key=True)
