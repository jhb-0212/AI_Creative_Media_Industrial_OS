"""restrict outbox grants: 审计事件 append-only，撤销应用角色的 DELETE

根因（决策 3 不可变审计 + 最小权限）：
- 迁移 9e3b4c5d6e7f 对 project_planning / outbox 两 schema 的 ALL TABLES
  统一授予 SELECT/INSERT/UPDATE/DELETE；outbox.outbox_events 是 append-only
  事件流，应用只需追加与标记投递（UPDATE published_at），不需要物理删除。
- 授予 DELETE 等于让应用路径可以抹掉审计证据，与「不可变事件」承诺冲突。

改动：
1. 撤销 app_role 对 outbox.outbox_events 的 DELETE；
2. 撤销 outbox schema 默认权限里的 DELETE，避免后续新建事件表再获得删除权。
（这是对已应用迁移的前向修正：不重写历史迁移，保证已部署库同样收敛。）

Revision ID: d4e5f6a7b8c9
Revises: b2c3d4e5f6a7
Create Date: 2026-11-14
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "d4e5f6a7b8c9"
down_revision: str | Sequence[str] | None = "b2c3d4e5f6a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "app_role"
OUTBOX_SCHEMA = "outbox"


def upgrade() -> None:
    """撤销 outbox 的删除权限（append-only 审计不可物理删除）。"""
    op.execute(f"REVOKE DELETE ON {OUTBOX_SCHEMA}.outbox_events FROM {APP_ROLE}")
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA {OUTBOX_SCHEMA} "
        f"REVOKE DELETE ON TABLES FROM {APP_ROLE}"
    )


def downgrade() -> None:
    """回滚：恢复 outbox 的删除权限与默认权限。"""
    op.execute(f"GRANT DELETE ON {OUTBOX_SCHEMA}.outbox_events TO {APP_ROLE}")
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA {OUTBOX_SCHEMA} "
        f"GRANT DELETE ON TABLES TO {APP_ROLE}"
    )
