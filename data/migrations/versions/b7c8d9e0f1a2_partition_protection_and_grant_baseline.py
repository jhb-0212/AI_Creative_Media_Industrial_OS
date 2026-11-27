"""outbox 分区保护 + 权限基线反转（默认零授权）

根因（不是"漏了几条 GRANT"，是权限模型方向错了）：
1. ``9e3b4c5d6e7f`` 用 ``ALTER DEFAULT PRIVILEGES`` 给 schema 里**未来所有表**
   自动授了 SELECT/INSERT/UPDATE/DELETE；``d4e5f6a7b8c9`` 只把 outbox 的
   DELETE 摘掉。于是每个新建分区天生带 SELECT/INSERT/UPDATE —— 直连分区成为
   一条**默认可用**的路径，tenant 隔离只剩父表策略这一层。
2. ``f6a7b8c9d1e2`` 只对父表 ``ENABLE ROW LEVEL SECURITY``；分区自身
   ``relrowsecurity=false``。通过父表查询受策略约束，直接点名分区则完全不受
   —— 实测 app_role 无租户上下文直查 ``outbox_events_y202611`` 读到跨租户行。

本迁移做三件事（只改权限与 RLS，不动数据）：
1. 对已存在分区：ENABLE RLS + 租户策略 + ``REVOKE ALL FROM app_role``（复用
   ``packages.shared.partition_guard``，与维护工具同一实现）；
2. 反转默认权限：三个应用 schema 取消"新表自动授权"，新表必须显式 GRANT
   （fail-closed：忘记授权会立刻报权限错误，而不是静默获得权限）；
3. 收紧 append-only 对象：``qc_reports`` 撤销 UPDATE/DELETE、
   ``asset_versions`` 撤销 DELETE（内容列不可变仍由后续 ADR 的触发器/列级
   授权完成，这里先关掉"整行删改"这个最大的口子）。
   附带：给 app_role 授 ``public.alembic_version`` 的 SELECT，使 ``/readyz``
   能回报 schema revision（用于识别"连到了另一个库实例"）。

Revision ID: b7c8d9e0f1a2
Revises: c1d2e3f4a5b6
Create Date: 2026-11-14
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from packages.shared.db_roles import app_role_name
from packages.shared.partition_guard import (
    OUTBOX_PARENT,
    OUTBOX_SCHEMA,
    protect_partition_statements,
    unprotect_partition_statements,
)

revision: str = "b7c8d9e0f1a2"
down_revision: str | Sequence[str] | None = "c1d2e3f4a5b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCHEMAS = ("project_planning", "outbox", "asset_service")
_ALEMBIC_VERSION = "public.alembic_version"

# 旧状态（downgrade 用）：9e3b4c5d6e7f 给 project_planning/outbox 的默认权限是
# INSERT/SELECT/UPDATE/DELETE；asset_service 由 c1d2e3f4a5b6 补成同样四项。
def _restore_default_grants() -> list[str]:
    """回滚时恢复"新表自动授权"的旧口径（仅本地回退使用）。"""
    statements: list[str] = []
    for schema in _SCHEMAS:
        statements.append(
            f"ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} "
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {app_role_name()}"
        )
    for schema in ("project_planning", "outbox"):
        statements.append(
            f"ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} "
            f"GRANT USAGE, SELECT ON SEQUENCES TO {app_role_name()}"
        )
    return statements


def _outbox_partitions() -> list[str]:
    """当前 outbox.outbox_events 的所有分区名（含 default 分区）。"""
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            """
            SELECT c.relname
            FROM pg_inherits i
            JOIN pg_class c ON c.oid = i.inhrelid
            JOIN pg_class p ON p.oid = i.inhparent
            JOIN pg_namespace n ON n.oid = p.relnamespace
            WHERE n.nspname = :schema AND p.relname = :parent
            ORDER BY c.relname
            """
        ),
        {"schema": OUTBOX_SCHEMA, "parent": OUTBOX_PARENT},
    ).scalars()
    return list(rows)


def upgrade() -> None:
    """分区保护到位 + 默认零授权 + append-only 授权收紧。"""
    # 1. 已存在分区：开 RLS、建策略、回收直连权限（幂等，重复执行无害）。
    for partition in _outbox_partitions():
        for statement in protect_partition_statements(partition):
            op.execute(statement)

    # 2. 默认权限反转：新表/新序列不再自动获得 app_role 权限。
    #    忘记显式 GRANT 的后果是权限错误（fail-closed），而不是静默放行。
    for schema in _SCHEMAS:
        op.execute(
            f"ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} "
            f"REVOKE ALL ON TABLES FROM {app_role_name()}"
        )
        op.execute(
            f"ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} "
            f"REVOKE ALL ON SEQUENCES FROM {app_role_name()}"
        )

    # 3. append-only：证据与版本表不接受应用的整行删改。
    op.execute(f"REVOKE DELETE ON asset_service.asset_versions FROM {app_role_name()}")
    op.execute(
        f"REVOKE UPDATE, DELETE ON asset_service.qc_reports FROM {app_role_name()}"
    )

    # 4. 让 /readyz 能读到 schema revision（只读元数据，不涉业务）。
    op.execute(f"GRANT SELECT ON {_ALEMBIC_VERSION} TO {app_role_name()}")


def downgrade() -> None:
    """回滚到"分区可直连、默认自动授权"的旧状态（仅用于本地回退）。"""
    op.execute(f"REVOKE SELECT ON {_ALEMBIC_VERSION} FROM {app_role_name()}")

    op.execute(f"GRANT DELETE ON asset_service.asset_versions TO {app_role_name()}")
    op.execute(f"GRANT UPDATE, DELETE ON asset_service.qc_reports TO {app_role_name()}")

    for statement in _restore_default_grants():
        op.execute(statement)

    for partition in _outbox_partitions():
        for statement in unprotect_partition_statements(partition):
            op.execute(statement)

    # WP-04/#14：父 revision 的 outbox 默认权限是 arw（无 DELETE）。
    # ``_restore_default_grants`` 沿用旧口径写成了 arwd，等于降级态多给出
    # DELETE；按父态精确收回，不接受"补回了一条 GRANT"就当成功。
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA outbox "
        f"REVOKE DELETE ON TABLES FROM {app_role_name()}"
    )
