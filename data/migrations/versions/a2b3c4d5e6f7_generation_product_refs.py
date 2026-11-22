"""GenerationTask 登记产物引用：asset_version_id / quality_profile_version。

为什么需要这两列
- QC 入队钩子是生成终态提交后的第二个事务；进程在两者之间崩溃时，任务已成功、
  队列里却没有待检项。没有持久化的产物引用，任何对账扫描都无法重建「检什么」。
- 只放在 Worker 内存里的产物引用随进程消失，等于「成功但不可追溯」。

为什么可为空串
- 历史成功任务与同步路径（GENERATION.REQUEST）没有产物引用，回填不可能凭空造；
  空串表示「未登记」，扫描时被排除，而不是伪装成已登记。

Revision ID: a2b3c4d5e6f7
Revises: f1a2b3c4d5e6
Create Date: 2026-11-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a2b3c4d5e6f7"
down_revision: str | Sequence[str] | None = "f1a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCHEMA = "production_planning"
_TABLE = "generation_tasks"


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column(
            "asset_version_id",
            sa.String(64),
            nullable=False,
            server_default="",
        ),
        schema=_SCHEMA,
    )
    op.add_column(
        _TABLE,
        sa.Column(
            "quality_profile_version",
            sa.String(64),
            nullable=False,
            server_default="",
        ),
        schema=_SCHEMA,
    )


def downgrade() -> None:
    op.drop_column(_TABLE, "quality_profile_version", schema=_SCHEMA)
    op.drop_column(_TABLE, "asset_version_id", schema=_SCHEMA)
