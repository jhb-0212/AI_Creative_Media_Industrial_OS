"""add tenant_id + RLS to outbox_events

根因（审查报告 P1-1 / R06）：
- Outbox 事件此前无 tenant_id，应用角色可读写 outbox 全表；多租户共用事件
  总线时，审计数据可被跨租户读取，租户隔离未闭环。
- 根治：事件表加 tenant_id（NOT NULL，回填默认租户）+ 与业务表相同的 RLS
  策略；应用以 app_role（NOBYPASSRLS）连接，事件写入/读取同样受租户隔离。

大表运维提示：本迁移在单个事务内完成 add_column(可空) → 全表回填 →
SET NOT NULL → 建索引。事件表增长后该组合会产生长事务与较重 IO，应在
维护窗口执行；数据量上规模后改为「可空列 + 索引 → 分批回填 → 单独 SET
NOT NULL」三步迁移。

Revision ID: b2c3d4e5f6a7
Revises: 9e3b4c5d6e7f
Create Date: 2026-11-14
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b2c3d4e5f6a7"
down_revision: str | Sequence[str] | None = "9e3b4c5d6e7f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "outbox"
_TABLE = "outbox_events"
_DEFAULT_TENANT = "00000000-0000-0000-0000-000000000000"
_INDEX = "ix_outbox_tenant"
_POLICY = "tenant_isolation_outbox_events"


def upgrade() -> None:
    """加 tenant_id 列 + 回填 + 收紧 + 索引 + RLS 策略。"""
    op.add_column(
        _TABLE, sa.Column("tenant_id", sa.Uuid(), nullable=True), schema=SCHEMA
    )
    op.execute(
        f"UPDATE {SCHEMA}.{_TABLE} SET tenant_id = '{_DEFAULT_TENANT}' "
        f"WHERE tenant_id IS NULL"
    )
    op.alter_column(_TABLE, "tenant_id", nullable=False, schema=SCHEMA)
    op.create_index(_INDEX, _TABLE, ["tenant_id"], unique=False, schema=SCHEMA)

    op.execute(f"ALTER TABLE {SCHEMA}.{_TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY {_POLICY}
        ON {SCHEMA}.{_TABLE}
        USING (
            tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid
        )
        """
    )


def downgrade() -> None:
    """回滚：删 RLS 策略 + 索引 + tenant_id 列。"""
    op.execute(f"DROP POLICY IF EXISTS {_POLICY} ON {SCHEMA}.{_TABLE}")
    op.execute(f"ALTER TABLE {SCHEMA}.{_TABLE} DISABLE ROW LEVEL SECURITY")
    op.drop_index(_INDEX, table_name=_TABLE, schema=SCHEMA)
    op.drop_column(_TABLE, "tenant_id", schema=SCHEMA)
