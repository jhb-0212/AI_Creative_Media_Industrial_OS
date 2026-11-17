"""生成执行域：generation_tasks（生成任务表，RLS + 状态机守卫）

Revision ID: b8c9d0e1f2a3
Revises: a5b6c7d8e9f0
Create Date: 2026-11-17

背景（Phase 1 主链段 3，ADR-R32）：
GENERATION.REQUEST 是主链"单一路线生成"的受理入口，契约（A6）已冻结
GenerationTask 对象，运行时却没有任何落点。本迁移补齐数据层：

1. generation_tasks 表（A6 冻结字段集）：生成任务是**项目级资源**
   （契约字段集带 project_id），RLS 走租户隔离 + 项目限制性策略
   （与镜头域 d5e6f7a8b9c0 同口径）。
2. 实现扩展列（契约 additionalProperties 允许，不出 required）：
   - retry_of：重试链锚点（9.4：重试以新任务承载，attempt+1）；
   - result_ref：worker 执行产出引用。
3. 状态机下沉数据库层（ADR-R20）：PENDING→RUNNING→SUCCEEDED/FAILED，
   转移表 + 触发器守卫；SUCCEEDED/FAILED 是终态，FAILED→RUNNING（复活
   重试）在数据库层直接被拒。started_at/completed_at/result_ref 是转移
   伴随元数据，纳入可变列白名单；attempt/retry_of 与全部准入引用列是
   不可变内容列——受理时定型的准入事实不可事后改写（ACT-GEN-NEG-001
   的防抵赖面）。
4. 授权口径：SELECT + INSERT + UPDATE（受理 + worker 生命周期推进），
   不授 DELETE（业务对象不可物理删除）。

转移字面量刻意写成模块常量而不是从代码导入：迁移是历史快照，若它随代码
变化，重放同一个 revision 会得到不同的库。一致性由
``tests/unit/test_generation_task_parity.py`` 比对这份字面量与当前
枚举/转移表来守。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from packages.shared.db_roles import DatabaseDomain, role_for

# revision identifiers, used by Alembic.
revision: str = "b8c9d0e1f2a3"
down_revision: str | Sequence[str] | None = "a5b6c7d8e9f0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "generation_execution"
TABLE = "generation_tasks"
OUTBOX_SCHEMA = "outbox"
ALEMBIC_VERSION = "public.alembic_version"

#: 契约声明的状态取值（迁移是历史快照，不 import 代码）。
GENERATION_TASK_STATUS_VALUES = ("PENDING", "RUNNING", "SUCCEEDED", "FAILED")

#: 播种用的状态转移边 —— 必须与领域转移表完全一致（9.4 / A6）。
GENERATION_TASK_STATUS_TRANSITIONS: tuple[tuple[str, str], ...] = (
    ("PENDING", "RUNNING"),
    ("RUNNING", "SUCCEEDED"),
    ("RUNNING", "FAILED"),
)

#: 状态/版本/时间戳/审计之外可变的列白名单（守卫用）：转移伴随元数据。
#: 白名单之外的列一律视为不可变内容列：受理时定型的准入事实
#: （project/shot/route/引用/attempt/retry_of）登记后不可改写。
_MUTABLE_COLUMNS: tuple[str, ...] = ("result_ref", "started_at", "completed_at")

_ORG_SETTING = "app.current_org_id"
_SCOPE_SETTING = "app.current_projects"


def _scope_expression(column: str) -> str:
    """项目归属判定表达式（与 d5e6f7a8b9c0 的策略文本保持同一口径）。"""
    return f"""(
        organization_id = NULLIF(current_setting('{_ORG_SETTING}', true), '')::uuid
        AND (
            current_setting('{_SCOPE_SETTING}', true) = '*'
            OR {column} = ANY (
                string_to_array(
                    NULLIF(current_setting('{_SCOPE_SETTING}', true), ''), ','
                )
            )
        )
    )"""


def _create_transitions_table() -> None:
    """建转移表（空壳，由调用方播种）。"""
    op.create_table(
        "generation_task_status_transitions",
        sa.Column("from_status", sa.String(32), primary_key=True, nullable=False),
        sa.Column("to_status", sa.String(32), primary_key=True, nullable=False),
        schema=SCHEMA,
    )


def _seed_transitions(edges: tuple[tuple[str, str], ...]) -> None:
    """播种已登记边。"""
    transitions = sa.table(
        "generation_task_status_transitions",
        sa.column("from_status", sa.String),
        sa.column("to_status", sa.String),
        schema=SCHEMA,
    )
    op.bulk_insert(
        transitions,
        [{"from_status": src, "to_status": tgt} for src, tgt in edges],
    )


def _create_guard() -> None:
    """建状态转移 + 不可变内容列守卫触发器（ADR-R20 范式，a5b6c7d8e9f0 同构）。

    - status 变化必须沿转移表登记过的边推进；
    - 不可变内容列：白名单之外的列值发生变化即拒绝；
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
                    SELECT 1 FROM {SCHEMA}.generation_task_status_transitions t
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
        # GenerationTask 业务列（契约字段，A6 冻结定义）
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("shot_id", sa.String(64), nullable=False),
        sa.Column("route_id", sa.String(64), nullable=False),
        sa.Column("shot_spec_ref", sa.String(64), nullable=False),
        sa.Column("visual_plan_ref", sa.String(64), nullable=False),
        sa.Column("constraint_snapshot_ref", sa.String(64), nullable=False),
        sa.Column("budget_reservation_ref", sa.String(64), nullable=False),
        sa.Column("quota_reservation_ref", sa.String(64), nullable=False),
        sa.Column("dependency_refs", postgresql.JSONB(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        # 实现扩展列（additionalProperties 允许）
        sa.Column("retry_of", sa.String(64), nullable=True),
        sa.Column("result_ref", sa.String(128), nullable=True),
        # worker 转移伴随元数据（可变白名单列）
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            f"status IN ({', '.join(repr(v) for v in GENERATION_TASK_STATUS_VALUES)})",
            name="ck_generation_execution_generation_tasks_status_registered",
        ),
        schema=SCHEMA,
    )

    # 索引名与 SQLAlchemy 自动命名一致（EntityMixin 的 tenant_id /
    # organization_id 带 index=True，业务列 project_id/shot_id 同范式）。
    op.create_index(
        f"ix_{SCHEMA}_{TABLE}_tenant_id", TABLE, ["tenant_id"], schema=SCHEMA
    )
    op.create_index(
        f"ix_{SCHEMA}_{TABLE}_organization_id",
        TABLE,
        ["organization_id"],
        schema=SCHEMA,
    )
    op.create_index(
        f"ix_{SCHEMA}_{TABLE}_project_id", TABLE, ["project_id"], schema=SCHEMA
    )
    op.create_index(
        f"ix_{SCHEMA}_{TABLE}_shot_id", TABLE, ["shot_id"], schema=SCHEMA
    )

    # 状态机：先建转移表并播种，再建守卫触发器（顺序反了守卫读不到边）。
    _create_transitions_table()
    _seed_transitions(GENERATION_TASK_STATUS_TRANSITIONS)
    _create_guard()

    # RLS：租户隔离 + 项目范围（限制性）策略（生成任务是项目级资源）。
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
    expression = _scope_expression("project_id")
    op.execute(
        f"""
        CREATE POLICY project_scope_{TABLE}
        ON {SCHEMA}.{TABLE}
        AS RESTRICTIVE
        FOR ALL
        USING ({expression})
        WITH CHECK ({expression})
        """
    )

    # 授权：本域角色可读、可受理、可推进生命周期；不授 DELETE。
    # 追加审计事件需要 outbox 的 SELECT/INSERT（与其它域一致）。
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON {SCHEMA}.{TABLE} TO {role}")
    op.execute(f"GRANT SELECT ON {SCHEMA}.generation_task_status_transitions TO {role}")
    op.execute(f"GRANT USAGE ON SCHEMA {OUTBOX_SCHEMA} TO {role}")
    op.execute(f"GRANT SELECT, INSERT ON {OUTBOX_SCHEMA}.outbox_events TO {role}")
    op.execute(f"GRANT SELECT ON {ALEMBIC_VERSION} TO {role}")


def downgrade() -> None:
    role = role_for(DatabaseDomain.GENERATION_EXECUTION)
    op.execute(f"REVOKE ALL ON {SCHEMA}.{TABLE} FROM {role}")
    op.execute(f"REVOKE ALL ON {SCHEMA}.generation_task_status_transitions FROM {role}")
    op.execute(f"REVOKE ALL ON {OUTBOX_SCHEMA}.outbox_events FROM {role}")
    op.execute(f"REVOKE ALL ON {ALEMBIC_VERSION} FROM {role}")
    for policy in (f"project_scope_{TABLE}", f"tenant_isolation_{TABLE}"):
        op.execute(f"DROP POLICY IF EXISTS {policy} ON {SCHEMA}.{TABLE}")
    op.execute(f"ALTER TABLE {SCHEMA}.{TABLE} DISABLE ROW LEVEL SECURITY")
    op.execute(f"DROP TRIGGER IF EXISTS {TABLE}_guard_trigger ON {SCHEMA}.{TABLE}")
    op.execute(f"DROP FUNCTION IF EXISTS {SCHEMA}.{TABLE}_guard()")
    op.drop_table("generation_task_status_transitions", schema=SCHEMA)
    op.drop_index(f"ix_{SCHEMA}_{TABLE}_shot_id", table_name=TABLE, schema=SCHEMA)
    op.drop_index(f"ix_{SCHEMA}_{TABLE}_project_id", table_name=TABLE, schema=SCHEMA)
    op.drop_index(f"ix_{SCHEMA}_{TABLE}_organization_id", table_name=TABLE, schema=SCHEMA)
    op.drop_index(f"ix_{SCHEMA}_{TABLE}_tenant_id", table_name=TABLE, schema=SCHEMA)
    op.drop_table(TABLE, schema=SCHEMA)
