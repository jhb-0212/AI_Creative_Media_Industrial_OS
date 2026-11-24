"""D6-9 死信人工重放：outbox.dead_letter_replays + outbox_ops_role（append-only）。

问题
----
``workers/relay/consumer.py`` 三处注释长期声称死信「可查、可人工重放」，
但仓库内没有任何重放端点、脚本或 CLI：行保留、指标可见（「可查」成立），
处置入口不存在（「可重放」不成立）。运维只能手工 SQL 改三列——操作者、
理由、重放前的失败次数全部不落纸，等于把「谁在什么时候放行了一条本该被
丢弃的事件」变成不可审计的动作。

为什么审计不落 outbox_events
----------------------------
重放由运维主体执行，而 ``outbox_relay_role`` 对 ``outbox_events`` 只有
SELECT + 列级 UPDATE、**没有 INSERT**。给它补 INSERT 等于允许伪造任意业务
事件（权限倒退）；业务域角色则刻意不持有 outbox 的 UPDATE。所以审计必须
落独立表：可写审计、不可写业务事件——这是权限边界，不是「多建一张表更保险」。

三件事必须在库层成立（都写在这里，而不是靠应用自觉）
----------------------------------------------------
1. **append-only**：``outbox_ops_role`` 只授 SELECT/INSERT；再加
   ``BEFORE UPDATE OR DELETE`` 与 ``BEFORE TRUNCATE`` 触发器（复用
   ``audit.reject_mutation`` / ``audit.reject_truncate``）。只做授权层时，
   超级用户或将来误配的角色仍能改写历史；只做触发器层时，「能不能写」
   不再由 GRANT 表达。两层同时存在，才既拦得住也说得清。
2. **租户隔离**：本表带 tenant_id，按与其他审计数据同一口径开 RLS + FORCE。
   运维动作不得跨租户：RLS 让跨租户行查不到（应用层给 404，而不是 403——
   不暴露「别处存在这条事件」）。
3. **事件内容不可篡改**：运维角色对 ``outbox_events`` 只授
   SELECT + ``UPDATE (attempts, dead_lettered_at, next_attempt_at)``，
   **不含 published_at**——投递时间只能由真正投递成功的 relay 写。
   授权由 ``db_grants.INFRA_ROLE_GRANTS`` 收敛（矩阵是执行器），
   分区上的同形授权由 ``partition_guard`` 统一生成。

分区授权为什么要动态 SQL
------------------------
分区名按月生成（``outbox_events_yYYYYMM``），静态迁移无从枚举；本仓库既有
口径是「动态对象不进静态矩阵，由 partition_guard 统一生成」。本迁移对
**现存分区**调用该生成器补齐授权，未来分区由 ``outbox_maintenance`` 在
``create_partition`` 里逐条授予（同一函数，单一事实源）。

Revision ID: c5d6e7f8a9b0
Revises: a7b8c9d0e1f2
Create Date: 2026-11-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from packages.shared.config import (
    get_settings,
    reject_development_default_password,
)
from packages.shared.db_grants import OUTBOX_OPS, infra_converge_statements
from packages.shared.db_roles import infra_role
from packages.shared.partition_guard import ops_partition_grant_do_block
from packages.shared.rls import tenant_scope_predicate

revision: str = "c5d6e7f8a9b0"
down_revision: str | Sequence[str] | None = "a7b8c9d0e1f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCHEMA = "outbox"
_TABLE = "dead_letter_replays"
_TABLES = frozenset({"outbox_events", _TABLE})


def _ops_password() -> str:
    """运维角色口令：只走 settings（生产由密钥管理注入），缺失即拒绝。

    与 relay 角色同一口径（``7a8b9c0d1e2f``）：迁移经裸 ``get_settings()``
    读配置、不声明进程身份，进程层的生产校验不会触发，因此必须在这里显式
    拒绝开发默认口令——否则生产迁移会以仓库里可预测的口令建出角色。
    """
    settings = get_settings()
    password = settings.postgres_outbox_ops_password.get_secret_value()
    if not password:
        raise RuntimeError(
            "POSTGRES_OUTBOX_OPS_PASSWORD 未配置：拒绝以空口令创建运维角色"
        )
    reject_development_default_password(
        settings, "postgres_outbox_ops_password", "POSTGRES_OUTBOX_OPS_PASSWORD"
    )
    return password.replace("'", "''")


def _create_role() -> None:
    """创建运维角色（已存在时同步口令并重置属性）。

    重置属性不是洁癖：只改口令会保留存量角色可能带有的 SUPERUSER / BYPASSRLS，
    角色隔离在那个环境里形同虚设（``7a8b9c0d1e2f`` 的同一教训）。
    """
    role = infra_role(OUTBOX_OPS)
    password = _ops_password()
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
                CREATE ROLE {role} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
                    NOBYPASSRLS PASSWORD '{password}';
            ELSE
                ALTER ROLE {role} WITH LOGIN NOSUPERUSER NOCREATEDB
                    NOCREATEROLE NOBYPASSRLS PASSWORD '{password}';
            END IF;
        END
        $$
        """
    )


def _create_table() -> None:
    op.create_table(
        _TABLE,
        # 操作身份，不是事件身份：标识「某人在某时对某条死信做过一次重放」。
        sa.Column("replay_id", sa.Uuid(), primary_key=True),
        sa.Column("source_event_id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        # 留一份事件类型：分区被摘除归档后，审计行仍要能回答「重放的是什么」。
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("operator", sa.String(128), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        # 重放**前**的失败事实：先写审计再清零 attempts，顺序反了这两个值永久丢失。
        sa.Column("attempts_before", sa.Integer(), nullable=False),
        sa.Column("dead_lettered_at_before", sa.DateTime(timezone=True), nullable=False),
        sa.Column("result", sa.String(16), nullable=False),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "result IN ('SUCCEEDED', 'FAILED')",
            name="ck_dead_letter_replays_result",
        ),
        schema=_SCHEMA,
    )
    op.create_index(
        op.f("ix_outbox_dead_letter_replays_source_event"),
        _TABLE,
        ["source_event_id"],
        schema=_SCHEMA,
    )
    op.create_index(
        op.f("ix_outbox_dead_letter_replays_tenant_created"),
        _TABLE,
        ["tenant_id", "created_at"],
        schema=_SCHEMA,
    )


def _append_only() -> None:
    """append-only 触发器（复用 audit schema 的两个通用函数）。

    不复用 ``audit.reject_mutation`` 的名字会让人以为这是两套规则；实际是同一
    条规则作用在第二张表上。函数存在性由 ``b8c9d0e1f2a3`` 之后的审计迁移保证。
    """
    op.execute(
        f"""
        CREATE TRIGGER trg_{_TABLE}_no_mutation
            BEFORE UPDATE OR DELETE ON {_SCHEMA}.{_TABLE}
            FOR EACH ROW EXECUTE FUNCTION audit.reject_mutation()
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER trg_{_TABLE}_no_truncate
            BEFORE TRUNCATE ON {_SCHEMA}.{_TABLE}
            FOR EACH STATEMENT EXECUTE FUNCTION audit.reject_truncate()
        """
    )


def _enable_rls() -> None:
    op.execute(f"ALTER TABLE {_SCHEMA}.{_TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation_{_TABLE} ON {_SCHEMA}.{_TABLE} "
        f"USING ({tenant_scope_predicate()})"
    )
    op.execute(f"ALTER TABLE {_SCHEMA}.{_TABLE} FORCE ROW LEVEL SECURITY")


def upgrade() -> None:
    _create_role()
    _create_table()
    _append_only()
    _enable_rls()
    # 父表授权由矩阵收敛（运维角色不是业务域，但同样不许手写 GRANT）。
    role = infra_role(OUTBOX_OPS)
    for statement in infra_converge_statements(role, OUTBOX_OPS, tables=_TABLES):
        op.execute(statement)
    # 现存分区补授权（动态对象，静态矩阵无法枚举；生成器在 partition_guard）。
    op.execute(ops_partition_grant_do_block())


def downgrade() -> None:
    """回滚：先摘权限再删角色，保证 upgrade → downgrade → upgrade 可往返。

    PG 拒绝删除仍持有对象权限的角色；顺序反了会停在
    "role cannot be dropped because some objects depend on it"。
    """
    role = infra_role(OUTBOX_OPS)
    op.execute(
        f"REVOKE ALL ON {_SCHEMA}.{_TABLE} FROM {role}"
    )
    op.execute(
        f"REVOKE ALL ON {_SCHEMA}.outbox_events FROM {role}"
    )
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA {_SCHEMA} "
        f"REVOKE ALL ON TABLES FROM {role}"
    )
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA {_SCHEMA} "
        f"REVOKE ALL ON SEQUENCES FROM {role}"
    )
    op.execute(f"REVOKE USAGE ON SCHEMA {_SCHEMA} FROM {role}")
    op.execute(
        f"DROP POLICY IF EXISTS tenant_isolation_{_TABLE} ON {_SCHEMA}.{_TABLE}"
    )
    op.execute(f"ALTER TABLE {_SCHEMA}.{_TABLE} DISABLE ROW LEVEL SECURITY")
    op.execute(f"DROP TRIGGER IF EXISTS trg_{_TABLE}_no_truncate ON {_SCHEMA}.{_TABLE}")
    op.execute(f"DROP TRIGGER IF EXISTS trg_{_TABLE}_no_mutation ON {_SCHEMA}.{_TABLE}")
    op.drop_index(
        op.f("ix_outbox_dead_letter_replays_tenant_created"),
        table_name=_TABLE,
        schema=_SCHEMA,
    )
    op.drop_index(
        op.f("ix_outbox_dead_letter_replays_source_event"),
        table_name=_TABLE,
        schema=_SCHEMA,
    )
    op.drop_table(_TABLE, schema=_SCHEMA)
    op.execute(f"DROP ROLE IF EXISTS {role}")
