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

from sqlalchemy import ForeignKeyConstraint, Index, String, UniqueConstraint
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
    """script_revisions 表模型（剧本版本，同属 project_planning schema）。

    归属完整性（决策 2）：
    - 复合外键 ``(project_id, episode_ref) → episodes(project_id, episode_ref)``
      在数据库层锁死「跨项目乱挂载」——任何写入的 (project_id, episode_ref)
      必须精确匹配一个真实存在的 Episode，杜绝仅靠 Service 查询校验的
      归属漏洞（R2 不变量放最可靠层级）。
    - 部分唯一索引 ux_script_active_version 兜底并发取代（见迁移 facd8d42d07f）。
    """

    __tablename__ = "script_revisions"

    # 业务字段（project_id + episode_ref 是复合外键的自然键列，非裸冗余）。
    project_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    episode_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)

    # 部分唯一索引：同项目 + 同集数下最多一个「非 SUPERSEDED」活跃版本。
    __table_args__ = (
        Index(
            "ux_script_active_version",
            "project_id",
            "episode_ref",
            unique=True,
            postgresql_where="status <> 'SUPERSEDED'",
        ),
        # 复合外键：强绑定 episodes(project_id, episode_ref)，防跨项目乱挂载。
        ForeignKeyConstraint(
            ["project_id", "episode_ref"],
            [f"{SCHEMA_NAME}.episodes.project_id", f"{SCHEMA_NAME}.episodes.episode_ref"],
            name="fk_script_revisions_episode",
        ),
        {"schema": SCHEMA_NAME},
    )


class EpisodeModel(EntityMixin, Base):
    """episodes 表模型（集数，归属 Project）。

    归属完整性（决策 2）：
    - ``(project_id, episode_ref)`` 唯一约束（自然键），是 script_revisions
      复合外键的引用目标，保证「同项目内 episode_ref 唯一、跨项目允许同名」。
    """

    __tablename__ = "episodes"
    __table_args__ = (
        # 自然键唯一约束：同项目内 episode_ref 唯一（复合外键的引用目标）。
        UniqueConstraint(
            "project_id",
            "episode_ref",
            name="ux_episode_ref_per_project",
        ),
        {"schema": SCHEMA_NAME},
    )

    # 业务字段
    project_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    episode_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
