"""生成执行域：generation_execution schema + 角色 + production_routes（RLS + 状态机守卫）

Revision ID: a5b6c7d8e9f0
Revises: d5e6f7a8b9c0
Create Date: 2026-11-17

背景（Phase 1 主链段 2，ADR-R31）：
PRODUCTION.ROUTE.SELECT 是主链"单一路线生成"的选路入口，契约（A6）已冻结
ProductionRoute 对象，运行时却没有任何落点。本迁移补齐数据层：

1. 新建独立 schema ``generation_execution`` + 独立数据库角色
   ``svc_generation_execution``（按域最小权限，ADR-R16 范式）：
   - 为什么不复用资产域角色：路线目录/生成任务是"生成流水线"职责，
     资产域角色代表"资产事实"。共用身份会让任务执行路径同时握有
     资产写入权——数据所有权边界在数据库层失效。
2. production_routes 表：路线适配器目录条目（A6 冻结字段集）。
   路线是**平台能力目录数据**，不是项目资源：契约字段集里没有
   project_id，RLS 只做租户隔离（选路时的项目上下文由授权层
   ResourceRef 绑定，不落在路线条目上）。
3. 状态机下沉数据库层（ADR-R20）：UNVERIFIED→VERIFIED 唯一登记边，
   转移表 + 触发器守卫；verified_by/verified_at 是转移伴随元数据
   （16.2：VERIFIED 必须有证据背书），纳入可变列白名单。
4. 授权口径：SELECT + INSERT + UPDATE（状态推进是唯一变更路径），
   不授 DELETE（业务对象不可物理删除）。

为什么没有 HTTP 状态变更入口：契约动作矩阵里 ProductionRoute 只有
PRODUCTION.ROUTE.SELECT（选路，不改路线状态）；UNVERIFIED→VERIFIED 的
标定入口是适配器验证工作流（16.2 与 EXTERNAL_TOOL.ADAPTER.VERIFY 同源
规则：集成工程负责人验证 + 安全/QC 独立复核），契约未给路线级 action——
不猜契约，不造入口。路线目录由运维播种，测试用仓储端口登记。

转移字面量刻意写成模块常量而不是从代码导入：迁移是历史快照，若它随代码
变化，重放同一个 revision 会得到不同的库。一致性由
``tests/unit/test_generation_route_parity.py`` 比对这份字面量与当前
枚举/转移表来守。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from packages.shared.config import get_settings
from packages.shared.db_roles import DatabaseDomain, role_for

# revision identifiers, used by Alembic.
revision: str = "a5b6c7d8e9f0"
down_revision: str | Sequence[str] | None = "d5e6f7a8b9c0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "generation_execution"
TABLE = "production_routes"
OUTBOX_SCHEMA = "outbox"
ALEMBIC_VERSION = "public.alembic_version"

#: 契约声明的状态取值（迁移是历史快照，不 import 代码）。
PRODUCTION_ROUTE_STATUS_VALUES = ("UNVERIFIED", "VERIFIED")

#: 播种用的状态转移边 —— 必须与领域转移表完全一致（A6 / 16.1-16.2）。
PRODUCTION_ROUTE_STATUS_TRANSITIONS: tuple[tuple[str, str], ...] = (
    ("UNVERIFIED", "VERIFIED"),
)

#: 状态/验证元数据之外不可变的列白名单（守卫用）。
#: 白名单之外的列一律视为不可变内容列：路线元数据（route_type / 控制信号 /
#: 外观引用 / QC 维度 / 能力证据引用）登记后不可改写——改写等于换了一条
#: 路线，必须重新走验证（C2 范式）。
_MUTABLE_COLUMNS: tuple[str, ...] = ("verified_by", "verified_at")

_ORG_SETTING = "app.current_org_id"
_SCOPE_SETTING = "app.current_projects"


def _scope_expression() -> str:
    """组织归属判定表达式（与 e7f8a9b0c1d2 的策略文本保持同一口径）。

    路线条目没有 project_id（平台能力目录），限制性策略按组织收口：
    组织内的路线目录对组织内全部项目可用。
    """
    return f"""(
        organization_id = NULLIF(current_setting('{_ORG_SETTING}', true), '')::uuid
    )"""


def _create_role(role: str) -> None:
    """创建域角色（幂等：已存在则同步口令，不改变其权限）。

    口令与其它域同源（``POSTGRES_APP_PASSWORD``）：口令轮换是密钥系统的事，
    而"同一口令、不同角色"已把爆炸半径限制在角色权限内。
    """
    password = get_settings().postgres_app_password.get_secret_value()
    if not password:
        raise RuntimeError(
            "POSTGRES_APP_PASSWORD 未配置：拒绝以空口令创建业务域角色"
        )
    escaped = password.replace("'", "''")
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
                CREATE ROLE {role} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
                    NOBYPASSRLS PASSWORD '{escaped}';
            ELSE
                ALTER ROLE {role} WITH PASSWORD '{escaped}';
            END IF;
        END
        $$
        """
    )


def _create_transitions_table() -> None:
    """建转移表（空壳，由调用方播种）。"""
    op.create_table(
        "production_route_status_transitions",
        sa.Column("from_status", sa.String(32), primary_key=True, nullable=False),
        sa.Column("to_status", sa.String(32), primary_key=True, nullable=False),
        schema=SCHEMA,
    )


def _seed_transitions(edges: tuple[tuple[str, str], ...]) -> None:
    """播种已登记边。"""
    transitions = sa.table(
        "production_route_status_transitions",
        sa.column("from_status", sa.String),
        sa.column("to_status", sa.String),
        schema=SCHEMA,
    )
    op.bulk_insert(
        transitions,
        [{"from_status": src, "to_status": tgt} for src, tgt in edges],
    )


def _create_guard() -> None:
    """建状态转移 + 不可变内容列守卫触发器（ADR-R20 范式，d5e6f7a8b9c0 同构）。

    - status 变化必须沿转移表登记过的边推进；
    - 不可变内容列：白名单之外的列值发生变化即拒绝，逐列扫描真实列，
      新增内容列自动纳入保护，不依赖手写黑名单；
    - DELETE 一律拒绝（业务对象不可物理删除）；
    - 触发器只挂 UPDATE/DELETE（对齐 d1e2f3a4b5c6 范式）：INSERT 时 OLD 为空
      行，status 合法性由 CHECK 约束与初始状态构造校验保证，不参与转移判定。
    """
    mutable = ", ".join(repr(c) for c in _MUTABLE_COLUMNS)
    op.execute(
        f"""
        CREATE FUNCTION {SCHEMA}.{TABLE}_guard()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            col text;
            old_status text;
            new_status text;
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION
                    '{SCHEMA}.{TABLE} 业务对象不可物理删除';
            END IF;

            old_status := to_jsonb(OLD) ->> 'status';
            new_status := to_jsonb(NEW) ->> 'status';
            IF old_status IS DISTINCT FROM new_status THEN
                IF NOT EXISTS (
                    SELECT 1 FROM {SCHEMA}.production_route_status_transitions t
                    WHERE t.from_status = old_status AND t.to_status = new_status
                ) THEN
                    RAISE EXCEPTION
                        '非法状态转移 % -> %（未登记，ADR-R20）',
                        old_status, new_status;
                END IF;
            END IF;

            FOR col IN
                SELECT attname
                FROM pg_attribute
                WHERE attrelid = '{SCHEMA}.{TABLE}'::regclass
                  AND attnum > 0
                  AND NOT attisdropped
            LOOP
                IF col IN ('status', 'entity_version', 'updated_at', 'audit_ref') THEN
                    CONTINUE;
                END IF;
                IF col IN ({mutable}) THEN
                    CONTINUE;
                END IF;
                IF (to_jsonb(OLD) ->> col) IS DISTINCT FROM (to_jsonb(NEW) ->> col) THEN
                    RAISE EXCEPTION
                        '{SCHEMA}.{TABLE}.% 为不可变内容列，禁止改写', col;
                END IF;
            END LOOP;

            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        f"CREATE TRIGGER {TABLE}_guard_trigger "
        f"BEFORE UPDATE OR DELETE ON {SCHEMA}.{TABLE} "
        f"FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.{TABLE}_guard()"
    )


def upgrade() -> None:
    role = role_for(DatabaseDomain.GENERATION_EXECUTION)

    # schema 必须先于表存在；IF NOT EXISTS 让重复执行不炸（迁移本身有版本表，
    # 这里只是防御"手工建过"的现场）。
    op.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
    _create_role(role)
    op.execute(f"GRANT USAGE ON SCHEMA {SCHEMA} TO {role}")

    op.create_table(
        TABLE,
        # C.3.1 通用实体信封列
        sa.Column("entity_id", sa.String(64), primary_key=True),
        sa.Column("entity_type", sa.String(64), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("entity_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("owner_module", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.String(64), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("audit_ref", sa.String(128), nullable=True),
        # ProductionRoute 业务列（契约字段，A6 冻结定义）
        sa.Column("route_type", sa.String(32), nullable=False),
        sa.Column("supported_control_signals", postgresql.JSONB(), nullable=False),
        sa.Column("required_appearance_refs", postgresql.JSONB(), nullable=False),
        sa.Column("qc_dimensions", postgresql.JSONB(), nullable=False),
        sa.Column("capability_refs", postgresql.JSONB(), nullable=False),
        # 验证元数据（16.2 证据背书；契约无此字段，内部列不出契约响应）
        sa.Column("verified_by", sa.String(64), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            f"status IN ({', '.join(repr(v) for v in PRODUCTION_ROUTE_STATUS_VALUES)})",
            name="ck_generation_execution_production_routes_status_registered",
        ),
        schema=SCHEMA,
    )

    # 索引名与 SQLAlchemy 自动命名一致（EntityMixin 的 tenant_id /
    # organization_id 带 index=True）。名字不一致会让 alembic check 报假漂移。
    op.create_index(
        f"ix_{SCHEMA}_{TABLE}_tenant_id", TABLE, ["tenant_id"], schema=SCHEMA
    )
    op.create_index(
        f"ix_{SCHEMA}_{TABLE}_organization_id",
        TABLE,
        ["organization_id"],
        schema=SCHEMA,
    )

    # 状态机：先建转移表并播种，再建守卫触发器（顺序反了守卫读不到边）。
    _create_transitions_table()
    _seed_transitions(PRODUCTION_ROUTE_STATUS_TRANSITIONS)
    _create_guard()

    # RLS：租户隔离 + 组织限制性策略（路线目录是平台能力数据，无项目列）。
    op.execute(f"ALTER TABLE {SCHEMA}.{TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY tenant_isolation_{TABLE}
        ON {SCHEMA}.{TABLE}
        USING (
            tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid
        )
        """
    )
    expression = _scope_expression()
    op.execute(
        f"""
        CREATE POLICY organization_scope_{TABLE}
        ON {SCHEMA}.{TABLE}
        AS RESTRICTIVE
        FOR ALL
        USING ({expression})
        WITH CHECK ({expression})
        """
    )

    # 授权：本域角色可读、可登记、可推进状态（验证标定走运维路径）；
    # 不授 DELETE。追加事件需要 outbox 的 SELECT/INSERT（与其它域一致）。
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON {SCHEMA}.{TABLE} TO {role}")
    op.execute(f"GRANT SELECT ON {SCHEMA}.production_route_status_transitions TO {role}")
    op.execute(f"GRANT USAGE ON SCHEMA {OUTBOX_SCHEMA} TO {role}")
    op.execute(f"GRANT SELECT, INSERT ON {OUTBOX_SCHEMA}.outbox_events TO {role}")
    op.execute(f"GRANT SELECT ON {ALEMBIC_VERSION} TO {role}")


def downgrade() -> None:
    role = role_for(DatabaseDomain.GENERATION_EXECUTION)
    op.execute(f"REVOKE ALL ON {SCHEMA}.{TABLE} FROM {role}")
    op.execute(f"REVOKE ALL ON {SCHEMA}.production_route_status_transitions FROM {role}")
    op.execute(f"REVOKE ALL ON {OUTBOX_SCHEMA}.outbox_events FROM {role}")
    op.execute(f"REVOKE ALL ON {ALEMBIC_VERSION} FROM {role}")
    for policy in (f"organization_scope_{TABLE}", f"tenant_isolation_{TABLE}"):
        op.execute(f"DROP POLICY IF EXISTS {policy} ON {SCHEMA}.{TABLE}")
    op.execute(f"ALTER TABLE {SCHEMA}.{TABLE} DISABLE ROW LEVEL SECURITY")
    op.execute(f"DROP TRIGGER IF EXISTS {TABLE}_guard_trigger ON {SCHEMA}.{TABLE}")
    op.execute(f"DROP FUNCTION IF EXISTS {SCHEMA}.{TABLE}_guard()")
    op.drop_table("production_route_status_transitions", schema=SCHEMA)
    op.drop_index(f"ix_{SCHEMA}_{TABLE}_organization_id", table_name=TABLE, schema=SCHEMA)
    op.drop_index(f"ix_{SCHEMA}_{TABLE}_tenant_id", table_name=TABLE, schema=SCHEMA)
    op.drop_table(TABLE, schema=SCHEMA)
    op.execute(f"REVOKE USAGE ON SCHEMA {SCHEMA} FROM {role}")
    op.execute(f"DROP SCHEMA IF EXISTS {SCHEMA}")
    # 角色保留（DROP ROLE 会因其它对象依赖而失败，且重建角色需要重新授权）；
    # 它已无任何对象权限，留着不影响隔离。
