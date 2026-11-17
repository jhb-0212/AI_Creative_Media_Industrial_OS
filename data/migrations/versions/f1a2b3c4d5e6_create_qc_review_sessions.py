"""质量治理域：qc_review_sessions（QC 复查会话，状态机守卫 + RLS）

Revision ID: f1a2b3c4d5e6
Revises: e1f2a3b4c5d6
Create Date: 2026-11-17

背景（Phase 1 验收 C5，ADR-R36 / 契约修订 V205-R14-A10）：
C.7 给了 QCReviewSession 状态链 OPEN→IN_REVIEW→DECISION_PENDING→RESOLVED→
GATE_ELIGIBLE→CLOSED，31.5 裁决 ④ 又明确禁止 RESOLVED→CLOSED 跳关，但运行时
没有这张表、也没有任何动作能推进它——"状态机存在、平台永远停在 OPEN"。
本迁移补数据层，四条性质必须在结构层成立：

1. **状态推进必须沿登记边**：转移表 + 守卫触发器（ADR-R20）。直连 SQL 的
   ``UPDATE ... SET status='CLOSED' WHERE status='RESOLVED'`` 会被数据库拒绝，
   不依赖任何应用代码记得校验——C5 的"跳关拒绝"因此有两个独立证据点。
2. **复查输入不可改写**：``findings`` / ``reference_set`` / 报告与资产版本引用
   属内容列，白名单之外的列一旦变化即 RAISE。允许事后换输入，等于允许把
   "复核了什么"改写成一个更好看的故事。
3. **结论追加、不删除**：``decisions`` 是可变列（追加），但表上没有 DELETE
   权限，守卫触发器对 DELETE 一律 RAISE；删会话等于抹掉证据链。
4. **一份报告只能开一次会话**：唯一约束 ``(tenant_id, project_id,
   qc_report_ref)``。两条互不相干的裁决链会让 Gate 无法判断信哪一条。

为什么不复用 ``gate_decisions`` 的 append-only 形态：会话有生命周期（状态必须
能推进），裁决没有。把两者塞进一张表只有两种结果——要么 Gate 裁决被迫可改，
要么会话状态被迫"每次新建一行"。ADR-R36 §2 记录了这条拆分。

状态取值与转移边刻意写成模块常量而不是 import 代码：迁移是历史快照，若它随
代码变化，重放同一个 revision 会得到不同的库。一致性由
``tests/unit/test_qc_review_parity.py`` 比对这份字面量与枚举/转移表来守。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from packages.shared.db_roles import DatabaseDomain, role_for

# revision identifiers, used by Alembic.
revision: str = "f1a2b3c4d5e6"
down_revision: str | Sequence[str] | None = "e1f2a3b4c5d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "qc_governance"
TABLE = "qc_review_sessions"
TRANSITIONS = "qc_review_session_status_transitions"
OUTBOX_SCHEMA = "outbox"
ALEMBIC_VERSION = "public.alembic_version"

#: 契约/枚举登记的全部取值（含 WAIVE_WITH_AUTHORITY：可表示、不可进入）。
QC_REVIEW_STATUS_VALUES: tuple[str, ...] = (
    "OPEN",
    "IN_REVIEW",
    "DECISION_PENDING",
    "RESOLVED",
    "GATE_ELIGIBLE",
    "CLOSED",
    "WAIVE_WITH_AUTHORITY",
)

#: 已登记转移边 —— 必须与领域转移表完全一致（C.7 + 31.5 裁决 ④）。
QC_REVIEW_STATUS_TRANSITIONS: tuple[tuple[str, str], ...] = (
    ("OPEN", "IN_REVIEW"),
    ("IN_REVIEW", "DECISION_PENDING"),
    ("DECISION_PENDING", "RESOLVED"),
    ("RESOLVED", "GATE_ELIGIBLE"),
    ("GATE_ELIGIBLE", "CLOSED"),
)

#: 状态推进时允许改写的列（结论追加与参与人登记）。
_MUTABLE_COLUMNS: tuple[str, ...] = ("decisions", "reviewers")

_ORG_SETTING = "app.current_org_id"
_SCOPE_SETTING = "app.current_projects"


def _scope_expression(column: str) -> str:
    """项目归属判定表达式（与 e8f9a0b1c2d3 / b8c9d0e1f2a3 同一口径）。"""
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
    """建转移表并播种（空表会让守卫拒绝一切推进）。"""
    op.create_table(
        TRANSITIONS,
        sa.Column("from_status", sa.String(32), primary_key=True, nullable=False),
        sa.Column("to_status", sa.String(32), primary_key=True, nullable=False),
        schema=SCHEMA,
    )
    transitions = sa.table(
        TRANSITIONS,
        sa.column("from_status", sa.String),
        sa.column("to_status", sa.String),
        schema=SCHEMA,
    )
    op.bulk_insert(
        transitions,
        [
            {"from_status": src, "to_status": tgt}
            for src, tgt in QC_REVIEW_STATUS_TRANSITIONS
        ],
    )


def _create_guard() -> None:
    """状态转移 + 不可变内容列 + 禁删除守卫（ADR-R20 范式）。"""
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
                    '{SCHEMA}.{TABLE} 是 QC 复查证据，禁止物理删除'
                    '（结论与缺陷是 Gate 追溯的依据）';
            END IF;

            old_status := to_jsonb(OLD) ->> 'status';
            new_status := to_jsonb(NEW) ->> 'status';
            IF old_status IS DISTINCT FROM new_status THEN
                IF NOT EXISTS (
                    SELECT 1 FROM {SCHEMA}.{TRANSITIONS} t
                    WHERE t.from_status = old_status AND t.to_status = new_status
                ) THEN
                    RAISE EXCEPTION
                        '非法状态转移 % -> %（未登记，31.5 / ADR-R20）',
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
    role = role_for(DatabaseDomain.QC_GOVERNANCE)

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
        # QCReviewSession 业务列（契约字段，A10 补 project_id）
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("asset_version_id", sa.String(64), nullable=False),
        sa.Column("qc_report_ref", sa.String(64), nullable=False),
        sa.Column("reference_set", postgresql.JSONB(), nullable=False),
        sa.Column("reviewers", postgresql.JSONB(), nullable=False),
        sa.Column("findings", postgresql.JSONB(), nullable=False),
        sa.Column("decisions", postgresql.JSONB(), nullable=False),
        sa.CheckConstraint(
            f"status IN ({', '.join(repr(v) for v in QC_REVIEW_STATUS_VALUES)})",
            name="ck_qc_governance_qc_review_sessions_status_registered",
        ),
        sa.CheckConstraint(
            "jsonb_array_length(findings) > 0",
            name="ck_qc_governance_qc_review_sessions_findings_not_empty",
        ),
        sa.CheckConstraint(
            "jsonb_array_length(reviewers) > 0",
            name="ck_qc_governance_qc_review_sessions_reviewers_not_empty",
        ),
        schema=SCHEMA,
    )

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
        f"ix_{SCHEMA}_{TABLE}_project", TABLE, ["project_id"], schema=SCHEMA
    )
    op.create_index(
        f"ix_{SCHEMA}_{TABLE}_asset_version",
        TABLE,
        ["asset_version_id"],
        schema=SCHEMA,
    )
    op.create_index(
        f"uq_{SCHEMA}_{TABLE}_report",
        TABLE,
        ["tenant_id", "project_id", "qc_report_ref"],
        unique=True,
        schema=SCHEMA,
    )

    # 状态机：先建转移表并播种，再建守卫（顺序反了守卫读不到边）。
    _create_transitions_table()
    _create_guard()

    # RLS：租户隔离 + 项目范围（限制性），与 gate_decisions 同口径。
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

    # 授权：本域角色可读、可开启（INSERT）、可推进（UPDATE）；不授 DELETE。
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON {SCHEMA}.{TABLE} TO {role}")
    op.execute(f"GRANT SELECT ON {SCHEMA}.{TRANSITIONS} TO {role}")
    op.execute(f"GRANT USAGE ON SCHEMA {OUTBOX_SCHEMA} TO {role}")
    op.execute(f"GRANT SELECT, INSERT ON {OUTBOX_SCHEMA}.outbox_events TO {role}")


def downgrade() -> None:
    role = role_for(DatabaseDomain.QC_GOVERNANCE)
    op.execute(f"REVOKE ALL ON {SCHEMA}.{TABLE} FROM {role}")
    op.execute(f"REVOKE ALL ON {SCHEMA}.{TRANSITIONS} FROM {role}")
    for policy in (f"project_scope_{TABLE}", f"tenant_isolation_{TABLE}"):
        op.execute(f"DROP POLICY IF EXISTS {policy} ON {SCHEMA}.{TABLE}")
    op.execute(f"ALTER TABLE {SCHEMA}.{TABLE} DISABLE ROW LEVEL SECURITY")
    op.execute(f"DROP TRIGGER IF EXISTS {TABLE}_guard_trigger ON {SCHEMA}.{TABLE}")
    op.execute(f"DROP FUNCTION IF EXISTS {SCHEMA}.{TABLE}_guard()")
    op.drop_table(TRANSITIONS, schema=SCHEMA)
    op.drop_index(f"uq_{SCHEMA}_{TABLE}_report", table_name=TABLE, schema=SCHEMA)
    op.drop_index(
        f"ix_{SCHEMA}_{TABLE}_asset_version", table_name=TABLE, schema=SCHEMA
    )
    op.drop_index(f"ix_{SCHEMA}_{TABLE}_project", table_name=TABLE, schema=SCHEMA)
    op.drop_index(
        f"ix_{SCHEMA}_{TABLE}_organization_id", table_name=TABLE, schema=SCHEMA
    )
    op.drop_index(f"ix_{SCHEMA}_{TABLE}_tenant_id", table_name=TABLE, schema=SCHEMA)
    op.drop_table(TABLE, schema=SCHEMA)
