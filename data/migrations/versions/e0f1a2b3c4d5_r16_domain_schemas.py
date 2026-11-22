"""R16 三域 schema 预埋：production_planning / budget_quota / qc_governance

为什么先建空 schema（而不是等到 cutover ``a0b1c2d3e4f5``）
------------------------------------------------------------
最小权限收敛迁移（``c1a2b3d4e5f6`` 及其后的运行时表迁移）经
``converge_statements`` 为每个域角色生成 ``ALTER DEFAULT PRIVILEGES IN SCHEMA
<domain>``；PG 要求被引用的 schema 已存在，clean install 在 cutover 迁移之前就会
抛 ``InvalidSchemaNameError: schema "production_planning" does not exist``。

本迁移只预埋空 schema：角色创建、建表、RLS 与表级授权仍归 ``a0b1c2d3e4f5``
（时间胶囊职责不变），这里不写任何 GRANT 字面量。

Revision ID: e0f1a2b3c4d5
Revises: e9f0a1b2c3d4
Create Date: 2026-11-22
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

from packages.shared.db_grants import (
    BUDGET_QUOTA,
    PRODUCTION_PLANNING,
    QC_GOVERNANCE,
)

revision: str = "e0f1a2b3c4d5"
down_revision: str | Sequence[str] | None = "e9f0a1b2c3d4"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_SCHEMAS = (PRODUCTION_PLANNING, BUDGET_QUOTA, QC_GOVERNANCE)


def upgrade() -> None:
    """预埋三个 R16 业务域的空 schema（幂等）。"""
    for schema in _SCHEMAS:
        op.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")


def downgrade() -> None:
    """回滚预埋：仅当 schema 仍为空（后续迁移已先被 downgrade）时可删除。"""
    for schema in reversed(_SCHEMAS):
        op.execute(f"DROP SCHEMA IF EXISTS {schema} RESTRICT")
