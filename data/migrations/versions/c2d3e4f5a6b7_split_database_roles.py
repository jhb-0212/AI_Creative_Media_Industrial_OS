"""把单一 app_role 拆成按域最小权限的数据库角色

根因（审查第三轮 F2「数据所有权」）：
- ``9e3b4c5d6e7f`` 只建了一个 ``app_role``，并对每个 schema
  ``GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES``。于是 API、Outbox
  Relay、分区维护共用同一身份：**任何一处代码都能写任何一张表**。
  模块边界只存在于 Python 目录里，数据库层完全不存在。
- ``b7c8d9e0f1a2`` 之后默认授权已反转为"零授权"，但已有表的权限没有按域
  重新划分，跨模块写表仍然畅通。

本迁移做的事（只改角色与授权，不动数据、不动表结构）：
1. 为每个业务域建一个 ``LOGIN`` / ``NOSUPERUSER`` / ``NOBYPASSRLS`` 角色，
   口令统一来自 ``POSTGRES_APP_PASSWORD``（生产由密钥系统注入）；
2. 按"该域真正要读写的表"逐条授权——不再有 ``ON ALL TABLES``；
   Outbox 的投递状态用**列级** UPDATE 授权，relay 拿不到事件正文的写权；
3. 对现有 outbox 分区回收全部业务角色的直连权限（不变量：只能走父表）；
4. 退役 ``app_role``：撤销一切权限后 DROP，让"回退到旧角色"这条路消失。

刻意不做的事：
- 不设 ``ALTER DEFAULT PRIVILEGES``：新表必须显式 GRANT。忘记授权会立刻
  报权限错误（fail-closed），而不是静默获得跨域写权。
- 不建 ``BYPASSRLS`` 或 owner 角色给应用：owner 在 PG 里天然绕过 RLS，
  等于把租户隔离重新关掉。

Revision ID: c2d3e4f5a6b7
Revises: a1b2c3d4e5f6
Create Date: 2026-11-16
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from packages.shared.db_roles import DatabaseDomain, all_business_roles, role_for
from packages.shared.partition_guard import (
    APP_ROLE,
    OUTBOX_PARENT,
    OUTBOX_SCHEMA,
    protect_partition_statements,
    unprotect_partition_statements,
)

revision: str = "c2d3e4f5a6b7"
down_revision: str | Sequence[str] | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PROJECT_SCHEMA = "project_planning"
ASSET_SCHEMA = "asset_service"
AUTH_SCHEMA = "auth"
ALEMBIC_VERSION = "public.alembic_version"

# 各 schema 下的业务表（授权逐表列出，避免 ALL TABLES）。
_PROJECT_TABLES = ("projects", "episodes", "script_revisions")
_ASSET_TABLES = ("asset_versions", "qc_reports")
_AUTH_TABLES = ("revoked_tokens",)

# relay 可以改写的列：投递状态，不含 payload / event_type / tenant_id 等事实列。
# 这是"事件正文只追加"在数据库层的表达——不需要应用层自觉。
_RELAY_UPDATABLE_COLUMNS = (
    "published_at",
    "attempts",
    "next_attempt_at",
    "dead_lettered_at",
)


def _domain_password() -> str:
    """域角色口令（与迁移 9e3b4c5d6e7f 同源，不新增口令变量）。

    为何不按域拆分口令：口令轮换是密钥系统的事，而"同一口令、不同角色"
    已经能把爆炸半径限制在角色权限内（拿到 relay 口令写不了业务表）。
    生产若要求一域一口令，由密钥系统注入不同值即可，本迁移不改。
    """
    from packages.shared.config import get_settings

    password = get_settings().postgres_app_password.get_secret_value()
    if not password:
        raise RuntimeError(
            "POSTGRES_APP_PASSWORD 未配置：拒绝以空口令创建业务域角色"
        )
    return password.replace("'", "''")


def _create_role(role: str, password: str) -> None:
    """创建域角色（幂等：已存在则同步口令，不改变其权限）。"""
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
                CREATE ROLE {role} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
                    NOBYPASSRLS PASSWORD '{password}';
            ELSE
                ALTER ROLE {role} WITH PASSWORD '{password}';
            END IF;
        END
        $$
        """
    )


def _revoke_role_everywhere(role: str) -> None:
    """把某角色在所有业务 schema 上的权限清空（退役/回滚共用）。

    顺序：默认权限 → 函数 → 表/序列 → schema USAGE。

    ``ALTER DEFAULT PRIVILEGES`` 必须先撤：默认权限记录会让 ``DROP ROLE`` 因
    "仍有对象依赖"而失败。函数也要撤——``outbox.list_active_tenants()`` 这类
    枚举函数是 ``a1b2c3d4e5f6`` 直接授给应用的，只撤表权限仍会留下依赖。
    """
    for schema in (PROJECT_SCHEMA, ASSET_SCHEMA, OUTBOX_SCHEMA, AUTH_SCHEMA):
        op.execute(
            f"ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} "
            f"REVOKE ALL ON TABLES FROM {role}"
        )
        op.execute(
            f"ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} "
            f"REVOKE ALL ON SEQUENCES FROM {role}"
        )
        op.execute(
            f"ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} "
            f"REVOKE ALL ON FUNCTIONS FROM {role}"
        )
        op.execute(f"REVOKE ALL ON ALL FUNCTIONS IN SCHEMA {schema} FROM {role}")
        op.execute(
            f"REVOKE ALL ON ALL TABLES IN SCHEMA {schema} FROM {role}"
        )
        op.execute(
            f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA {schema} FROM {role}"
        )
        op.execute(f"REVOKE ALL ON SCHEMA {schema} FROM {role}")
    op.execute(f"REVOKE ALL ON {ALEMBIC_VERSION} FROM {role}")


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


def _grant_readiness_metadata(role: str) -> None:
    """``/readyz`` 要能读 schema revision（只读元数据，不涉业务表）。

    每个域角色都需要：就绪探针不能因为"连的是哪个域"而时好时坏。
    """
    op.execute(f"GRANT SELECT ON {ALEMBIC_VERSION} TO {role}")


def upgrade() -> None:
    """建立按域最小权限角色，并退役跨 schema 的 app_role。"""
    password = _domain_password()

    # 1. 建四个域角色（项目规划 / 资产 / Outbox 投递 / 身份）。
    for domain in DatabaseDomain:
        _create_role(role_for(domain), password)

    project_role = role_for(DatabaseDomain.PROJECT_PLANNING)
    asset_role = role_for(DatabaseDomain.ASSET)
    relay_role = role_for(DatabaseDomain.OUTBOX_RELAY)
    identity_role = role_for(DatabaseDomain.IDENTITY)

    # 2. 项目规划域：本域三表全量读写；能向 outbox 追加事件（事务性 outbox）。
    op.execute(f"GRANT USAGE ON SCHEMA {PROJECT_SCHEMA} TO {project_role}")
    for table in _PROJECT_TABLES:
        op.execute(
            f"GRANT SELECT, INSERT, UPDATE, DELETE "
            f"ON {PROJECT_SCHEMA}.{table} TO {project_role}"
        )
    op.execute(f"GRANT USAGE ON SCHEMA {OUTBOX_SCHEMA} TO {project_role}")
    op.execute(
        f"GRANT SELECT, INSERT ON {OUTBOX_SCHEMA}.outbox_events TO {project_role}"
    )

    # 3. 资产域：资产版本可改不可删、QC 报告只追加；同样能追加 outbox 事件。
    #    这两条比"全量 CRUD"更严，是因为 b7c8d9e0f1a2 已确立 append-only
    #    语义——本迁移把它从"撤销权限"升级为"按域显式授予正确权限"。
    op.execute(f"GRANT USAGE ON SCHEMA {ASSET_SCHEMA} TO {asset_role}")
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE "
        f"ON {ASSET_SCHEMA}.asset_versions TO {asset_role}"
    )
    op.execute(
        f"GRANT SELECT, INSERT ON {ASSET_SCHEMA}.qc_reports TO {asset_role}"
    )
    op.execute(f"GRANT USAGE ON SCHEMA {OUTBOX_SCHEMA} TO {asset_role}")
    op.execute(
        f"GRANT SELECT, INSERT ON {OUTBOX_SCHEMA}.outbox_events TO {asset_role}"
    )

    # 4. Outbox Relay：读事件 + 只改投递状态列。
    #    列级授权让"改事件正文"在数据库层成为不可能，而不是靠代码不这么写。
    #    不给 outbox_maintenance_runs：维护记录只由维护 CLI（超级用户）读写。
    op.execute(f"GRANT USAGE ON SCHEMA {OUTBOX_SCHEMA} TO {relay_role}")
    op.execute(f"GRANT SELECT ON {OUTBOX_SCHEMA}.outbox_events TO {relay_role}")
    columns = ", ".join(_RELAY_UPDATABLE_COLUMNS)
    op.execute(
        f"GRANT UPDATE ({columns}) ON {OUTBOX_SCHEMA}.outbox_events TO {relay_role}"
    )
    # 租户枚举函数是 relay 的扫描入口（a1b2c3d4e5f6 曾直接授给 app_role，
    # 角色拆分后必须显式授给新角色，否则 relay 启动第一轮就报无权限）。
    op.execute(
        f"GRANT EXECUTE ON FUNCTION {OUTBOX_SCHEMA}.list_active_tenants() "
        f"TO {relay_role}"
    )

    # 5. 身份域：只能碰吊销表。认证阶段的吊销检查走这个角色，
    #    业务域因此不需要任何 auth schema 权限。
    op.execute(f"GRANT USAGE ON SCHEMA {AUTH_SCHEMA} TO {identity_role}")
    for table in _AUTH_TABLES:
        op.execute(
            f"GRANT SELECT, INSERT ON {AUTH_SCHEMA}.{table} TO {identity_role}"
        )

    # 6. 全部域角色可读就绪元数据。
    for role in all_business_roles():
        _grant_readiness_metadata(role)

    # 7. 现有 outbox 分区：回收全部业务角色的直连权限。
    #    父表权限足以让 relay/project 通过父表读写，分区直连必须不可用。
    for partition in _outbox_partitions():
        for statement in protect_partition_statements(
            partition, roles=all_business_roles()
        ):
            op.execute(statement)

    # 8. 退役 app_role：先清权限再 DROP，确保"回退到旧角色"这条路不存在。
    _revoke_role_everywhere(APP_ROLE)
    op.execute(f"DROP ROLE IF EXISTS {APP_ROLE}")


def downgrade() -> None:
    """回滚：重建 app_role 的旧口径授权，并删除四个域角色。

    注意（集群级约束，不是本迁移的缺陷）：PostgreSQL 角色是**集群级**对象，
    而授权是**库级**对象。若同一集群里还有别的库对这些角色持有授权，
    ``DROP ROLE`` 会以 "objects depend on it" 拒绝。因此本回退只适用于
    「整个集群只有这一个业务库」的形态；多库共用集群时要先在其它库里清理
    依赖，或直接重建集群。这条限制写在这里，是为了让下一次遇到它的人不必
    重新推导一遍。
    """
    password = _domain_password()

    # 1. 重建 app_role 与旧授权（8f2a1b3c4d5e / 9e3b4c5d6e7f 的状态）。
    _create_role(APP_ROLE, password)
    for schema in (PROJECT_SCHEMA, OUTBOX_SCHEMA, ASSET_SCHEMA):
        op.execute(f"GRANT USAGE ON SCHEMA {schema} TO {APP_ROLE}")
        op.execute(
            f"GRANT SELECT, INSERT, UPDATE, DELETE "
            f"ON ALL TABLES IN SCHEMA {schema} TO {APP_ROLE}"
        )
        op.execute(
            f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {schema} TO {APP_ROLE}"
        )
    op.execute(f"GRANT SELECT ON {AUTH_SCHEMA}.revoked_tokens TO {APP_ROLE}")
    op.execute(f"GRANT SELECT ON {ALEMBIC_VERSION} TO {APP_ROLE}")
    # append-only 语义不随角色回滚而放开（b7c8d9e0f1a2 的结论保持有效）。
    op.execute(f"REVOKE DELETE ON {ASSET_SCHEMA}.asset_versions FROM {APP_ROLE}")
    op.execute(f"REVOKE UPDATE, DELETE ON {ASSET_SCHEMA}.qc_reports FROM {APP_ROLE}")
    op.execute(f"REVOKE DELETE ON {OUTBOX_SCHEMA}.outbox_events FROM {APP_ROLE}")

    # 2. 分区恢复"可直连"（对应 app_role 的旧状态）。
    for partition in _outbox_partitions():
        for statement in unprotect_partition_statements(
            partition, roles=(APP_ROLE,)
        ):
            op.execute(statement)

    # 3. 删除域角色（先清权限，否则依赖会让 DROP 失败）。
    for domain in DatabaseDomain:
        role = role_for(domain)
        _revoke_role_everywhere(role)
        op.execute(f"DROP ROLE IF EXISTS {role}")
