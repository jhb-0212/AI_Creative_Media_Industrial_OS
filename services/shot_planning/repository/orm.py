"""Shot ORM 模型（映射到独立 schema：shot_planning）。

数据所有权（ADR-R27）：
- shot_planning 模块拥有 ShotDraft / ShotRevision 表的写入权。
- 使用独立 schema「shot_planning」，与 project_planning 彻底隔离。
- 表结构严格对齐 C.3.1 通用实体信封字段（信封列收敛到 EntityMixin）。
"""

from __future__ import annotations

from sqlalchemy import Index, String, Text
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
