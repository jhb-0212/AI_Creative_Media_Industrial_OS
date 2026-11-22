"""create episodes table

Revision ID: 0fcaf4db4573
Revises: facd8d42d07f
Create Date: 2026-11-13 15:43:39.533981

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0fcaf4db4573"
down_revision: str | Sequence[str] | None = "facd8d42d07f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 与 orm.py 保持一致的 schema 名（数据所有权边界）。
SCHEMA_NAME = "project_planning"


def upgrade() -> None:
    """创建 episodes 表（集数，归属 Project）。"""
    op.create_table(
        "episodes",
        sa.Column("entity_id", sa.String(64), primary_key=True),
        sa.Column("entity_type", sa.String(64), nullable=False),
        sa.Column("schema_version", sa.Integer, nullable=False),
        sa.Column("entity_version", sa.Integer, nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("owner_module", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.String(64), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("audit_ref", sa.String(128), nullable=True),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("episode_ref", sa.String(64), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        schema=SCHEMA_NAME,
    )
    # 同项目内 episode_ref 唯一（集数归属约束的数据库兜底）。
    op.create_index(
        "ux_episode_ref_per_project",
        "episodes",
        ["project_id", "episode_ref"],
        unique=True,
        schema=SCHEMA_NAME,
    )
    # project_id 普通索引（ORM 的 index=True 对应，命名遵循 op.f 约定）。
    op.create_index(
        op.f("ix_project_planning_episodes_project_id"),
        "episodes",
        ["project_id"],
        unique=False,
        schema=SCHEMA_NAME,
    )


def downgrade() -> None:
    """回滚：删除 episodes 表及其索引。"""
    op.drop_index(
        op.f("ix_project_planning_episodes_project_id"),
        table_name="episodes",
        schema=SCHEMA_NAME,
    )
    op.drop_index("ux_episode_ref_per_project", table_name="episodes", schema=SCHEMA_NAME)
    op.drop_table("episodes", schema=SCHEMA_NAME)
