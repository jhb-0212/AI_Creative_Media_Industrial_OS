"""relay 列级 UPDATE 授权收口（审查 P1-01：事件业务内容不可篡改）

根因（不是「漏了几条 REVOKE」，是授权模型把「回写投递状态」错授成「整表可改」）：
- 7a8b9c0d1e2f 对父表 outbox.outbox_events 授 ``GRANT SELECT, UPDATE``，
  ae8724980e0b 对已存在分区同样授整表 UPDATE。整表 UPDATE 意味着 relay 能
  改写 payload / event_type / tenant_id / aggregate_type / occurred_at 等
  事件业务内容——被攻破的 relay 进程即可篡改已投递审计事件，直接违背
  「事件 append-only 不可篡改」红线（F03 数据所有权 + 决策 3）。
- 审计事件只有两类合法回写：ack 写 published_at，失败重试写
  attempts / next_attempt_at / dead_lettered_at（见 workers/relay/consumer.py
  的 _ACK / _FAIL）。relay 从不需要、也不该能改事件业务内容。

根治（列级授权下沉到数据库层，非 Python 代码自觉）：
- 撤销整表 UPDATE，改授列级 ``UPDATE (published_at, attempts,
  next_attempt_at, dead_lettered_at)``。此后 relay 若试图
  ``UPDATE ... SET payload=...`` 会在数据库层被拒绝
  （permission denied for column），RLS/角色隔离之外再补一层「内容不可改」。
- 三层收口：父表 + 已存在分区 + ALTER DEFAULT PRIVILEGES（未来分区）。
  列清单是唯一事实源 RELAY_UPDATE_COLUMNS（packages/shared/partition_guard.py），
  迁移与维护工具共用 relay_column_update_statements，杜绝三份拷贝漂移。

Revision ID: b9e1c2d3f4a5
Revises: ae8724980e0b
Create Date: 2026-11-18
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from packages.shared.partition_guard import (
    OUTBOX_PARENT,
    OUTBOX_SCHEMA,
    RELAY_UPDATE_COLUMNS,
    quote_ident,
    relay_column_update_statements,
)

# revision identifiers, used by Alembic.
revision: str = "b9e1c2d3f4a5"
down_revision: str | Sequence[str] | None = "ae8724980e0b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LIST_PARTITIONS = sa.text(
    """
    SELECT c.relname
    FROM pg_inherits i
    JOIN pg_class c ON c.oid = i.inhrelid
    JOIN pg_class p ON p.oid = i.inhparent
    JOIN pg_namespace n ON n.oid = p.relnamespace
    WHERE n.nspname = :schema AND p.relname = :parent
    ORDER BY c.relname
    """
)


def _existing_partitions() -> list[str]:
    bind = op.get_bind()
    rows = bind.execute(
        _LIST_PARTITIONS, {"schema": OUTBOX_SCHEMA, "parent": OUTBOX_PARENT}
    ).scalars()
    return list(rows)


def _relay_role() -> str:
    from packages.shared.config import get_settings

    return get_settings().postgres_relay_user


def upgrade() -> None:
    """父表 + 已存在分区 + 未来分区：整表 UPDATE 收窄为列级 UPDATE。"""
    relay_role = _relay_role()
    # 1. 父表：撤销整表 UPDATE，改授列级。
    for statement in relay_column_update_statements(OUTBOX_PARENT):
        op.execute(statement)

    # 2. 已存在分区：同样收口（通过父表访问时 PG 仍校验目标分区权限）。
    for partition in _existing_partitions():
        for statement in relay_column_update_statements(partition):
            op.execute(statement)

    # 3. 未来分区：默认权限从「整表 UPDATE」改为「列级 UPDATE」。
    #    ALTER DEFAULT PRIVILEGES 不支持列级授权（其 GRANT 无列列表），
    #    因此撤销整表 UPDATE 的默认授权，未来分区由 outbox_maintenance 建分区
    #    时显式调用 grant_outbox_partition_statements（已改为列级 UPDATE）补上
    #    列级授权——授权时机与「建分区」同一动作，不依赖默认权限兜底。
    op.execute(
        f"ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA {OUTBOX_SCHEMA} "
        f"REVOKE UPDATE ON TABLES FROM {quote_ident(relay_role)}"
    )


def downgrade() -> None:
    """回滚：恢复整表 UPDATE（回到「relay 可改任意列」的旧态，仅本地回退用）。"""
    relay_role = _relay_role()
    parent = f"{quote_ident(OUTBOX_SCHEMA)}.{quote_ident(OUTBOX_PARENT)}"
    # 撤销列级 UPDATE，恢复整表 UPDATE。
    columns = ", ".join(quote_ident(c) for c in RELAY_UPDATE_COLUMNS)
    op.execute(f"REVOKE UPDATE ({columns}) ON {parent} FROM {quote_ident(relay_role)}")
    op.execute(f"GRANT UPDATE ON {parent} TO {quote_ident(relay_role)}")

    for partition in _existing_partitions():
        qualified = f"{quote_ident(OUTBOX_SCHEMA)}.{quote_ident(partition)}"
        op.execute(f"REVOKE UPDATE ({columns}) ON {qualified} FROM {quote_ident(relay_role)}")
        op.execute(f"GRANT UPDATE ON {qualified} TO {quote_ident(relay_role)}")

    op.execute(
        f"ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA {OUTBOX_SCHEMA} "
        f"GRANT UPDATE ON TABLES TO {quote_ident(relay_role)}"
    )
