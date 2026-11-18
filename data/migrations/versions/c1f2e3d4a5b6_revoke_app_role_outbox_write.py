"""回收 app_role 对 outbox 的写权限（审查 P1-02 / Review Thread R9）

根因（角色生命周期无完整闭环，旧角色残留成为绕过新边界的入口）：
- 迁移 9e3b4c5d6e7f 创建 app_role 时授予 project_planning + outbox 两个
  schema 的 USAGE + 表读写 + 序列权限。
- 迁移 8f3b2c1d4e5a（DATA-1 拆域角色）回收了 app_role 对 project_planning /
  asset_service / auth 的权限，但**遗漏了 outbox**。于是 app_role 至今仍持有
  ``outbox.outbox_events`` 的 INSERT + UPDATE——即「旧应用角色仍能写事件」。
- 事件总线（outbox）是平台级审计事实，写入权已收归 project_planning_role /
  asset_service_role（业务域同事务投递）与 outbox_relay_role（回写投递状态）。
  app_role 不再承担任何生产写路径，其残留的 outbox INSERT/UPDATE 成为一条
  「绕过新域角色边界、直接伪造/篡改审计事件」的入口。

根治（回收旧角色越界写权限，而非再加一层校验）：
- 回收 app_role 对 outbox.outbox_events 的 INSERT + UPDATE；保留 SELECT——
  app_role 仍是「RLS 生效验证角色」与「分区直连拒绝验证角色」（见
  test_tenant_isolation_invariants.py），需能通过父表读事件验证 fail-closed。
- 同时回收 outbox schema 默认权限里的 INSERT/UPDATE，杜绝未来新建事件表
  再次自动获得 app_role 写权（与 b7c8d9e0f1a2 的「默认零授权」口径一致）。
- DELETE 已由 d4e5f6a7b8c9 回收，maintenance_runs 已由 b5c6d7e8f9a0 回收，
  本迁移补齐最后一环「outbox_events 的写权限」，使 app_role 的 outbox 权限
  收敛为「只读」。

Revision ID: c1f2e3d4a5b6
Revises: b9e1c2d3f4a5
Create Date: 2026-11-18
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

from packages.shared.db_roles import app_role_name

# revision identifiers, used by Alembic.
revision: str = "c1f2e3d4a5b6"
down_revision: str | Sequence[str] | None = "b9e1c2d3f4a5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
OUTBOX_SCHEMA = "outbox"
OUTBOX_TABLE = "outbox_events"


def upgrade() -> None:
    """回收 app_role 对 outbox_events 的写权限（保留 SELECT）。"""
    op.execute(
        f"REVOKE INSERT, UPDATE ON {OUTBOX_SCHEMA}.{OUTBOX_TABLE} FROM {app_role_name()}"
    )
    # 未来新建事件表：默认权限不再自动授予 app_role 写权。
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA {OUTBOX_SCHEMA} "
        f"REVOKE INSERT, UPDATE ON TABLES FROM {app_role_name()}"
    )


def downgrade() -> None:
    """回滚：恢复 app_role 对 outbox_events 的写权限（仅本地回退用）。"""
    op.execute(
        f"GRANT INSERT, UPDATE ON {OUTBOX_SCHEMA}.{OUTBOX_TABLE} TO {app_role_name()}"
    )
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA {OUTBOX_SCHEMA} "
        f"GRANT INSERT, UPDATE ON TABLES TO {app_role_name()}"
    )
