"""add outbox tenant enumeration function: relay 的窄跨租户通道

根因（R06 消费端，决策见 docs/adr/ADR-R06-outbox-relay-tenancy.md）：
- outbox.outbox_events 受 RLS 保护，app_role 只能看到 `app.current_tenant_id`
  指定租户的事件。后台 relay 因此无法「一次读全表」，这是刻意的。
- 若给 relay 建 BYPASSRLS 角色（方案 A），等于把越权读通道重新打开：该角色
  被误用或攻破即可读全部租户的审计负载。
- 采用方案 B：relay 用与 API 相同的 app_role 逐租户消费；跨租户能力收敛为
  本函数——**只返回 tenant_id 与积压统计，不返回任何 payload**。泄露面是
  「有哪些租户」，不是「租户里发生了什么」。

设计约束：
1. SECURITY DEFINER：函数以迁移角色（表 owner）身份执行，从而能跨过 RLS；
2. SET search_path：避免调用方通过 search_path 劫持函数内的对象解析；
3. REVOKE FROM PUBLIC 后仅授 app_role：PostgreSQL 默认把 EXECUTE 授给
   PUBLIC，不收回等于所有角色都能调；
4. 只枚举**有未投递事件**的租户：空租户不该出现在 relay 的每轮扫描里。

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-11-14
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "e5f6a7b8c9d0"
down_revision: str | Sequence[str] | None = "d4e5f6a7b8c9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "app_role"
OUTBOX_SCHEMA = "outbox"

_CREATE_FUNCTION = """
CREATE FUNCTION outbox.list_active_tenants()
RETURNS TABLE (
    tenant_id uuid,
    pending_count bigint,
    oldest_occurred_at timestamptz
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = outbox, pg_catalog
AS $fn$
    SELECT e.tenant_id, count(*)::bigint, min(e.occurred_at)
    FROM outbox.outbox_events AS e
    WHERE e.published_at IS NULL
    GROUP BY e.tenant_id
$fn$
"""


def upgrade() -> None:
    """创建租户枚举函数并只授权给应用角色。"""
    op.execute(_CREATE_FUNCTION)
    op.execute(
        f"REVOKE ALL ON FUNCTION {OUTBOX_SCHEMA}.list_active_tenants() FROM PUBLIC"
    )
    op.execute(
        f"GRANT EXECUTE ON FUNCTION {OUTBOX_SCHEMA}.list_active_tenants() "
        f"TO {APP_ROLE}"
    )


def downgrade() -> None:
    """回滚：删除枚举函数（relay 将失去跨租户入口，按 fail-closed 停摆）。"""
    op.execute(f"DROP FUNCTION IF EXISTS {OUTBOX_SCHEMA}.list_active_tenants()")
