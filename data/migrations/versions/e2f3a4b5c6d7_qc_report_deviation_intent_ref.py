"""qc_reports 增加 deviation_intent_ref（对齐 QCReport 契约的既有字段）

Revision ID: e2f3a4b5c6d7
Revises: d1e2f3a4b5c6
Create Date: 2026-11-14

背景（审计 P1-3 字段级漂移）：
契约 `object_schemas.schema.json#QCReport.properties` 声明了
`deviation_intent_ref`，但运行时全仓 0 命中——契约里有、代码里没有，
响应里自然永远不可能出现。字段漂移不能靠"以后有人用再说"，它会让下游
（偏差归因、意图比对）误以为这个证据一直没被采集。

这里补齐存储与序列化；它是可选字段（nullable），不改变既有行的合法性，
也不进 payload 必填集（事件契约已登记 payload_required，未包含该字段）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e2f3a4b5c6d7"
down_revision: str | Sequence[str] | None = "d1e2f3a4b5c6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "qc_reports",
        sa.Column("deviation_intent_ref", sa.String(length=64), nullable=True),
        schema="asset_service",
    )


def downgrade() -> None:
    op.drop_column("qc_reports", "deviation_intent_ref", schema="asset_service")
