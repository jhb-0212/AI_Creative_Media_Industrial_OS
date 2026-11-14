"""create outbox.maintenance_runs (保留期维护的审计与新鲜度表)

根因（③ 自动化闭环缺口）：
- 分区维护是手工 CLI，没人跑就等于没做。判断「上次成功是什么时候」不能靠某人
  记得，必须有一张可查询的表；
- 维护动作本身（建分区 / 归档 / 摘除 / 拒绝）也需要留痕：拒绝摘除是一个真实事件，
  不能只出现在 stdout 里。

设计约束：
1. 只记录**维护**事实（时间、结果计数、状态、错误摘要），不记录任何业务 payload；
2. 本表不属于租户数据，不启用 RLS；也不授给 app_role——应用路径没有理由读它；
3. 状态取值收敛为 ok / refused / error / dry_run，便于告警直接判「最近 26 小时内
   没有 ok」。

Revision ID: b8c9d1e2f3a4
Revises: f6a7b8c9d1e2
Create Date: 2026-11-14
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b8c9d1e2f3a4"
down_revision: str | Sequence[str] | None = "f6a7b8c9d1e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "outbox"
_TABLE = "maintenance_runs"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("applied", sa.Boolean(), nullable=False),
        sa.Column("created", sa.Integer(), nullable=False),
        sa.Column("detached", sa.Integer(), nullable=False),
        sa.Column("refused", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_outbox_maintenance_runs_started",
        _TABLE,
        ["started_at"],
        unique=False,
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_outbox_maintenance_runs_started", table_name=_TABLE, schema=SCHEMA
    )
    op.drop_table(_TABLE, schema=SCHEMA)
