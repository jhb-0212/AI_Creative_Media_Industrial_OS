"""Outbox relay 重试与死信字段（P2-2）

Revision ID: f3a4b5c6d7e8
Revises: e2f3a4b5c6d7
Create Date: 2026-11-14

背景（审计 P2-2）：
relay 的 handler 抛异常只 `failed += 1` 后 `continue`，没有 attempts 计数、
没有退避、没有死信。结果是：
- 毒事件每一轮都被立刻重领，单条坏事件可以永久拖慢该租户的投递；
- 失败次数不可见，无法区分"偶发失败"和"永远失败"；
- 没有终点，积压里永远混着永远不会成功的事件。

本迁移加三列（对分区父表 ADD COLUMN 会自动下传到所有分区）：
- attempts：已失败次数；
- next_attempt_at：下一次可被领取的时间（指数退避）；
- dead_lettered_at：进入死信的时间（非空即不再被领取）。

死信不删除：事件是审计事实，死信只代表"投递放弃"，仍可人工 inspect/重放。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f3a4b5c6d7e8"
down_revision: str | Sequence[str] | None = "e2f3a4b5c6d7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "outbox_events",
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        schema="outbox",
    )
    op.add_column(
        "outbox_events",
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        schema="outbox",
    )
    op.add_column(
        "outbox_events",
        sa.Column("dead_lettered_at", sa.DateTime(timezone=True), nullable=True),
        schema="outbox",
    )


def downgrade() -> None:
    op.drop_column("outbox_events", "dead_lettered_at", schema="outbox")
    op.drop_column("outbox_events", "next_attempt_at", schema="outbox")
    op.drop_column("outbox_events", "attempts", schema="outbox")
