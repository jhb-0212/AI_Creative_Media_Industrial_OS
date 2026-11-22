"""fix outbox tenant enumeration: 死信不再计入 backlog（审计复核 R2）

背景（复核 docs/audits/2026-11-16-code-and-iron-law-review.md 的 R2）：
e5f6a7b8c9d0 建的 `outbox.list_active_tenants()` 只按 `published_at IS NULL`
统计 pending_count，把两类「不可领取」的事件也算进了积压：
- 死信（dead_lettered_at 非空，领取 SQL 已排除，见 consumer._CLAIM）；
- 未到重试时间（next_attempt_at > now()，指数退避中）。

后果：一条事件进入死信后 pending_count 永不下降，OUTBOX_RELAY_BACKLOG 报警
永远消不掉；该租户也永远留在扫描列表里每轮白扫一次 claim。运维无法区分
「真积压」与「已放弃的死信」。

本迁移把 `list_active_tenants()` 的统计口径与 `_CLAIM` 的领取口径对齐：
- pending_count 只统计「可领取」事件（published_at IS NULL
  AND dead_lettered_at IS NULL AND next_attempt_at <= now()）；
- 新增 dead_count / oldest_dead_at，死信单独观测（不再混入 backlog）；
- oldest_occurred_at 也只对应可领取事件（与 pending_count 同口径）。

Revision ID: a1b2c3d4e5f6
Revises: c6d7e8f9a0b1
Create Date: 2026-11-16
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

from packages.shared.db_roles import app_role_name

revision: str = "a1b2c3d4e5f6"
down_revision: str | Sequence[str] | None = "c6d7e8f9a0b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
OUTBOX_SCHEMA = "outbox"

# 与 worker relay 的 _CLAIM 领取口径保持一致：只有「可领取」的事件才计入
# pending_count，死信与退避中的事件分别用 dead_count 单独观测。
# 注意：本迁移改变了函数返回类型（新增 dead_count / oldest_dead_at），
# PostgreSQL 禁止 CREATE OR REPLACE 改变返回类型，故必须先 DROP 再 CREATE。
_CREATE_FUNCTION = """
CREATE FUNCTION outbox.list_active_tenants()
RETURNS TABLE (
    tenant_id uuid,
    pending_count bigint,
    dead_count bigint,
    oldest_occurred_at timestamptz,
    oldest_dead_at timestamptz
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = outbox, pg_catalog
AS $fn$
    SELECT
        e.tenant_id,
        count(*) FILTER (
            WHERE e.published_at IS NULL
              AND e.dead_lettered_at IS NULL
              AND e.next_attempt_at <= now()
        )::bigint AS pending_count,
        count(*) FILTER (
            WHERE e.published_at IS NULL
              AND e.dead_lettered_at IS NOT NULL
        )::bigint AS dead_count,
        min(e.occurred_at) FILTER (
            WHERE e.published_at IS NULL
              AND e.dead_lettered_at IS NULL
              AND e.next_attempt_at <= now()
        ) AS oldest_occurred_at,
        min(e.dead_lettered_at) FILTER (
            WHERE e.published_at IS NULL
              AND e.dead_lettered_at IS NOT NULL
        ) AS oldest_dead_at
    FROM outbox.outbox_events AS e
    WHERE e.published_at IS NULL
    GROUP BY e.tenant_id
$fn$
"""


def upgrade() -> None:
    """重定义枚举函数：pending_count 与领取口径对齐，新增死信统计。"""
    # 返回类型变更必须 DROP 后重建，CREATE OR REPLACE 无法改返回类型。
    op.execute(f"DROP FUNCTION IF EXISTS {OUTBOX_SCHEMA}.list_active_tenants()")
    op.execute(_CREATE_FUNCTION)
    op.execute(
        f"REVOKE ALL ON FUNCTION {OUTBOX_SCHEMA}.list_active_tenants() FROM PUBLIC"
    )
    op.execute(
        f"GRANT EXECUTE ON FUNCTION {OUTBOX_SCHEMA}.list_active_tenants() "
        f"TO {app_role_name()}"
    )


def downgrade() -> None:
    """回滚到 e5f6a7b8c9d0 的口径（pending_count 重新包含死信）。"""
    op.execute(f"DROP FUNCTION IF EXISTS {OUTBOX_SCHEMA}.list_active_tenants()")
    op.execute(
        """
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
    )
    op.execute(
        f"REVOKE ALL ON FUNCTION {OUTBOX_SCHEMA}.list_active_tenants() FROM PUBLIC"
    )
    op.execute(
        f"GRANT EXECUTE ON FUNCTION {OUTBOX_SCHEMA}.list_active_tenants() "
        f"TO {app_role_name()}"
    )
