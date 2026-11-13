"""create outbox_events table

根因（审查报告 P0-3 / R7）：
- 三个领域对象共用一个 audit_ref 字段，状态转移时直接覆盖；第二次状态
  变更丢失第一次的审计引用，无法追溯完整历史。
- 决策 3：Transactional Outbox——业务状态变更与审计事件同一事务原子写入，
  事件 append-only 不可变，可完整恢复审批/取代历史。

Revision ID: 7c8d9e0f1a2b
Revises: 6a7b8c9d0e1f
Create Date: 2026-11-13
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "7c8d9e0f1a2b"
down_revision: str | Sequence[str] | None = "6a7b8c9d0e1f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "outbox"


def upgrade() -> None:
    """创建 outbox schema 与 outbox_events 表。"""
    op.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
    op.create_table(
        "outbox_events",
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("aggregate_type", sa.String(64), nullable=False),
        sa.Column("aggregate_id", sa.String(64), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("event_id"),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_outbox_aggregate",
        "outbox_events",
        ["aggregate_id"],
        unique=False,
        schema=SCHEMA,
    )
    op.create_index(
        "ix_outbox_published",
        "outbox_events",
        ["published_at"],
        unique=False,
        schema=SCHEMA,
    )


def downgrade() -> None:
    """回滚：删表 + schema。"""
    op.drop_index("ix_outbox_published", table_name="outbox_events", schema=SCHEMA)
    op.drop_index("ix_outbox_aggregate", table_name="outbox_events", schema=SCHEMA)
    op.drop_table("outbox_events", schema=SCHEMA)
    op.execute(f"DROP SCHEMA IF EXISTS {SCHEMA}")
