"""add tenant_id + requested_by columns and enable RLS

根因（审查报告 P0-3 身份治理零落地）：
- 三个核心表（projects/episodes/script_revisions）无 tenant_id，多租户
  隔离完全缺失，``SELECT * FROM projects`` 能读到所有租户数据。
- 决策 5：所有核心表加 tenant_id（UUID），配置 PostgreSQL RLS 行级安全，
  ``USING (tenant_id = current_setting('app.current_tenant_id')::uuid)``，
  越权漏洞按死在数据库层。
- 两步审批流（身份治理单元）：script_revisions 加 requested_by 列，
  承载「锁定申请人真实主体」，批准动作据此做职责分离比对。

改动：
1. projects / episodes / script_revisions 加 tenant_id（UUID，NOT NULL，
   先回填存量数据再收紧）。
2. script_revisions 加 requested_by（String(64)，可空）。
3. 三表启用 RLS + 策略（tenant_id 隔离）。

Revision ID: 8f2a1b3c4d5e
Revises: 7c8d9e0f1a2b
Create Date: 2026-11-13
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8f2a1b3c4d5e"
down_revision: str | Sequence[str] | None = "7c8d9e0f1a2b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "project_planning"

# 存量数据回填用的默认租户 UUID（历史数据归属单一默认租户，后续可再迁移）。
_DEFAULT_TENANT = "00000000-0000-0000-0000-000000000000"

_TABLES = ("projects", "episodes", "script_revisions")


def upgrade() -> None:
    """加 tenant_id / requested_by 列 + 启用 RLS。"""
    # 1. 三表加 tenant_id（先可空，回填后再收紧为 NOT NULL）。
    for table in _TABLES:
        op.add_column(table, sa.Column("tenant_id", sa.Uuid(), nullable=True), schema=SCHEMA)

    # 2. 回填存量数据：历史数据归属单一默认租户。
    for table in _TABLES:
        op.execute(
            f"UPDATE {SCHEMA}.{table} SET tenant_id = '{_DEFAULT_TENANT}' "
            f"WHERE tenant_id IS NULL"
        )

    # 3. 收紧 tenant_id 为 NOT NULL + 建索引（RLS 隔离键，查询频繁）。
    #    索引命名遵循 op.f 约定（带 schema 前缀，与 ORM index=True 自动命名一致）。
    for table in _TABLES:
        op.alter_column(table, "tenant_id", nullable=False, schema=SCHEMA)
        op.create_index(
            op.f(f"ix_project_planning_{table}_tenant_id"),
            table,
            ["tenant_id"],
            unique=False,
            schema=SCHEMA,
        )

    # 4. script_revisions 加 requested_by（锁定申请人，可空）。
    op.add_column(
        "script_revisions",
        sa.Column("requested_by", sa.String(64), nullable=True),
        schema=SCHEMA,
    )

    # 5. 启用 RLS + tenant 隔离策略（三表）。
    #    NULLIF(current_setting(...), '') 把「未定义」与「空串」都归一为 NULL，
    #    tenant_id = NULL 恒为 false → 未认证/未注入租户的会话看不到任何行
    #    （fail-closed 防呆），且避免 ''::uuid 的类型转换错误。
    for table in _TABLES:
        op.execute(f"ALTER TABLE {SCHEMA}.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation_{table}
            ON {SCHEMA}.{table}
            USING (
                tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid
            )
            """
        )


def downgrade() -> None:
    """回滚：删 RLS 策略 + requested_by + tenant_id 列。"""
    for table in _TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation_{table} ON {SCHEMA}.{table}")
        op.execute(f"ALTER TABLE {SCHEMA}.{table} DISABLE ROW LEVEL SECURITY")

    op.drop_column("script_revisions", "requested_by", schema=SCHEMA)

    for table in _TABLES:
        op.drop_index(
            op.f(f"ix_project_planning_{table}_tenant_id"),
            table_name=table,
            schema=SCHEMA,
        )
        op.drop_column(table, "tenant_id", schema=SCHEMA)
