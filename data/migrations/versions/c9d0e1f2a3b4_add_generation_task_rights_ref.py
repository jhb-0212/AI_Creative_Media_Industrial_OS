"""生成执行域：generation_tasks 补 rights_manifest_ref（产出权利凭据引用）

Revision ID: c9d0e1f2a3b4
Revises: b8c9d0e1f2a3
Create Date: 2026-11-17

背景（Phase 1 主链段 4，ADR-R33）：
段 4 把 worker 产出登记为资产版本（ASSET.VERSION.CREATE 的领域侧入口）。
资产域的批准硬约束要求 ``rights_manifest_ref`` 非空（无权利证据不得批准，
见 AssetService.approve），因此"生成产出 → 资产 → 批准"这条链上缺少一个
权利凭据的载体：缺它，机器产出的资产永远进不了批准链。

本迁移只加一列、不加约束：
- 可空：权利引用缺失是**合法状态**（产出照常登记，只是不可批准）。加
  NOT NULL 会逼受理端伪造一个权利引用，那比留空危险得多；
- 进不可变内容列：受理时定型的准入事实事后不可改写。b8c9d0e1f2a3 的守卫
  触发器按"白名单之外的列一律不可变"实现（逐列遍历 pg_attribute），因此
  新列**无需改触发器**即自动受保护——可变白名单仍是 result_ref /
  started_at / completed_at 三项；
- 授权不需要变更：列在同一张表，svc_generation_execution 的表级
  SELECT/INSERT/UPDATE 授权已覆盖新增列。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c9d0e1f2a3b4"
down_revision: str | Sequence[str] | None = "b8c9d0e1f2a3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "generation_execution"
TABLE = "generation_tasks"


def upgrade() -> None:
    op.add_column(
        TABLE,
        sa.Column("rights_manifest_ref", sa.String(64), nullable=True),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_column(TABLE, "rights_manifest_ref", schema=SCHEMA)
