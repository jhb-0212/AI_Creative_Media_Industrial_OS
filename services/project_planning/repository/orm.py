"""Project ORM 模型（映射到独立 schema：project_planning）。

数据所有权（14.2）：
- project_planning 模块拥有 Project / Episode / ScriptRevision 表的写入权。
- 使用独立 schema「project_planning」，避免与其它模块耦合。
- 表结构严格对齐 C.3.1 通用实体信封字段（信封列收敛到 EntityMixin）。

设计要点（消除重复机制 R4）：
- 三个模型重复声明的 C.3.1 信封列（entity_id/entity_type/schema_version/
  entity_version/status/owner_module/created_at/created_by/updated_at/audit_ref）
  收敛到 repository/entity_mixin.py 的 EntityMixin。
- 各模型的业务列（project_name/project_tier/title 等）保留在各自类中。
"""

from __future__ import annotations

from sqlalchemy import Index, String
from sqlalchemy.orm import Mapped, mapped_column

from packages.shared.database import Base
from services.project_planning.repository.entity_mixin import EntityMixin

# 独立 schema 名称（数据所有权边界）。
SCHEMA_NAME = "project_planning"


class ProjectModel(EntityMixin, Base):
    """projects 表模型。"""

    __tablename__ = "projects"
    __table_args__ = {"schema": SCHEMA_NAME}

    # 业务字段
    project_name: Mapped[str] = mapped_column(String(200), nullable=False)
    project_tier: Mapped[str] = mapped_column(String(8), nullable=False)


class ScriptModel(EntityMixin, Base):
    """script_revisions 表模型（剧本版本，同属 project_planning schema）。"""

    __tablename__ = "script_revisions"

    # 业务字段
    project_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    episode_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)

    # 部分唯一索引：同项目 + 同集数下最多一个「非 SUPERSEDED」活跃版本，
    # 作为并发取代的数据库兜底（见迁移 facd8d42d07f）。
    __table_args__ = (
        Index(
            "ux_script_active_version",
            "project_id",
            "episode_ref",
            unique=True,
            postgresql_where="status <> 'SUPERSEDED'",
        ),
        {"schema": SCHEMA_NAME},
    )


class EpisodeModel(EntityMixin, Base):
    """episodes 表模型（集数，归属 Project）。"""

    __tablename__ = "episodes"
    __table_args__ = (
        # 同项目内 episode_ref 唯一（集数归属约束的数据库兜底）。
        # 跨项目允许相同 episode_ref（如 EP001 在不同项目各自独立）。
        Index(
            "ux_episode_ref_per_project",
            "project_id",
            "episode_ref",
            unique=True,
        ),
        {"schema": SCHEMA_NAME},
    )

    # 业务字段
    project_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    episode_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
