"""grant outbox partitions to domain roles（修复 DATA-1/F03 拆角色后的分区授权缺口）

根因（系统性，非补丁）：
- outbox.outbox_events 是按月分区表（f6a7b8c9d1e2）。分区的创建分两路：
  迁移 f6a7b8c9d1e2（历史月份）+ workers/outbox_maintenance（未来月份）。
- 历史上授权模型只围绕单一 app_role：依赖 ``ALTER DEFAULT PRIVILEGES``
  给新分区自动授 app_role，再由 ``protect_partition_statements`` 对其
  ``REVOKE ALL`` 收口（直查分区绕过租户隔离的旧缺陷）。
- DATA-1（8f3b2c1d4e5a）拆出 project_planning_role / asset_service_role，
  F03（7a8b9c0d1e2f）拆出 outbox_relay_role 后，这三个角色的授权只在
  **父表** outbox_events 上；PG 通过父表访问分区时仍会校验**目标分区**的
  权限，而分区从未给这些角色授权——结果是业务域写事件、relay 领取事件
  全部报 ``permission denied for table outbox_events``（真实 PG 集成测试
  tests/integration/test_outbox_relay.py 首跑即暴露，此前因无 PG 环境被掩盖）。

根治（授权模型从「app_role 单角色」升级为「按访问类型分角色」）：
1. 对**已存在**的 outbox 分区，按访问类型补授权：
   - project_planning_role / asset_service_role：SELECT + INSERT（写事件、读本域事件）；
   - outbox_relay_role：SELECT + UPDATE（领取 + 回写 published_at）。
2. 用 ``ALTER DEFAULT PRIVILEGES`` 让**未来**新分区自动继承上述授权，与
   outbox_maintenance 建分区的路径解耦（即使维护工具漏调授权，新分区也不会
   静默无权限；fail-closed 由权限错误体现，而不是静默放行）。

授权口径与父表口径（8f3b2c1d4e5a 的 _grant_outbox_write / 7a8b9c0d1e2f 的
GRANT SELECT, UPDATE）严格一致：不授 DELETE（append-only 审计）、不授
app_role（其分区直连权限已收口）、relay 不授 INSERT（事件只能由业务域同事务
写入，relay 不得伪造事件）。

Revision ID: ae8724980e0b
Revises: 8f3b2c1d4e5a
Create Date: 2026-11-18
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from packages.shared.partition_guard import (
    OUTBOX_PARENT,
    OUTBOX_SCHEMA,
    grant_outbox_partition_statements,
    quote_ident,
    revoke_outbox_partition_statements,
)

# revision identifiers, used by Alembic.
revision: str = "ae8724980e0b"
down_revision: str | Sequence[str] | None = "8f3b2c1d4e5a"
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
    """当前 outbox_events 的所有分区名（含 default 分区）。"""
    bind = op.get_bind()
    rows = bind.execute(
        _LIST_PARTITIONS, {"schema": OUTBOX_SCHEMA, "parent": OUTBOX_PARENT}
    ).scalars()
    return list(rows)


def _default_privileges_for_role(role: str, privileges: str) -> str:
    """构造「未来新分区自动授权」的默认权限语句（针对 postgres 创建者）。"""
    return (
        f"ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA {OUTBOX_SCHEMA} "
        f"GRANT {privileges} ON TABLES TO {quote_ident(role)}"
    )


def upgrade() -> None:
    """对已有分区补授权 + 对未来分区建默认授权（按访问类型分角色）。"""
    from packages.shared.config import get_settings

    settings = get_settings()
    project_role = settings.postgres_project_planning_user
    asset_role = settings.postgres_asset_service_user
    relay_role = settings.postgres_relay_user

    # 1. 已存在分区：按访问类型补授权（幂等）。
    for partition in _existing_partitions():
        for statement in grant_outbox_partition_statements(partition):
            op.execute(statement)

    # 2. 未来分区：默认权限让 postgres 创建新分区时自动授权（双保险，与
    #    outbox_maintenance 的显式授权路径解耦）。
    op.execute(_default_privileges_for_role(project_role, "SELECT, INSERT"))
    op.execute(_default_privileges_for_role(asset_role, "SELECT, INSERT"))
    op.execute(_default_privileges_for_role(relay_role, "SELECT, UPDATE"))


def downgrade() -> None:
    """回滚：撤销分区授权 + 撤销默认授权（恢复「分区只授 app_role 后被收口」旧态）。"""
    from packages.shared.config import get_settings

    settings = get_settings()
    project_role = settings.postgres_project_planning_user
    asset_role = settings.postgres_asset_service_user
    relay_role = settings.postgres_relay_user

    for partition in _existing_partitions():
        for statement in revoke_outbox_partition_statements(partition):
            op.execute(statement)

    op.execute(
        f"ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA {OUTBOX_SCHEMA} "
        f"REVOKE SELECT, INSERT ON TABLES FROM {quote_ident(project_role)}"
    )
    op.execute(
        f"ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA {OUTBOX_SCHEMA} "
        f"REVOKE SELECT, INSERT ON TABLES FROM {quote_ident(asset_role)}"
    )
    op.execute(
        f"ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA {OUTBOX_SCHEMA} "
        f"REVOKE SELECT, UPDATE ON TABLES FROM {quote_ident(relay_role)}"
    )
