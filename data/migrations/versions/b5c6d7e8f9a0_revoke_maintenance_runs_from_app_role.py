"""收回 app_role 对 outbox.maintenance_runs 的数据权限（P3-5）

Revision ID: b5c6d7e8f9a0
Revises: a4b5c6d7e8f9
Create Date: 2026-11-14

现状（S1 实测）：`outbox.maintenance_runs` 无 RLS，且 app_role 持有
INSERT / SELECT / UPDATE。含义是：
- 应用进程（app_role）可以伪造/篡改保留期维护记录，也能读到全量运维元数据；
- 该表没有租户字段，RLS 无从约束——"无 RLS + 有写权限"叠在一起就是敞口。

该表只由 `workers/outbox_maintenance` 写入，而该 worker 用的正是迁移角色
（build_migration_url），不需要 app_role 的写权限；读侧运维也走迁移角色。
因此收回全部 app_role 权限，而不是给它加 RLS（无租户键的表加 RLS 只是
假装隔离）。

downgrade 恢复 S1 之前的授权面（INSERT/SELECT/UPDATE），保持可回滚。
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "b5c6d7e8f9a0"
down_revision: str | Sequence[str] | None = "a4b5c6d7e8f9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCHEMA = "outbox"
_TABLE = "maintenance_runs"
_APP_ROLE = "app_role"


def upgrade() -> None:
    op.execute(f'REVOKE ALL ON TABLE "{_SCHEMA}"."{_TABLE}" FROM {_APP_ROLE}')


def downgrade() -> None:
    op.execute(
        f'GRANT SELECT, INSERT, UPDATE ON TABLE "{_SCHEMA}"."{_TABLE}" '
        f"TO {_APP_ROLE}"
    )
