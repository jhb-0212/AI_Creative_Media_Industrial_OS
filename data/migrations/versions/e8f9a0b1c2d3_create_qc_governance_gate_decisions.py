"""质量治理域：qc_governance schema + 角色 + gate_decisions（append-only，RLS）

Revision ID: e8f9a0b1c2d3
Revises: d6e7f8a9b0c1
Create Date: 2026-11-16

背景（包 D：Phase 1 主链与交付闸门）：
`PRODUCTION.GATE.APPROVE` 是契约里唯一的 Gate 动作，运行时却没有任何落点——
意味着"Gate4 PASS"这件事在平台上**从未真实发生过**，而 `DeliveryManifest`
的 APPROVE/RELEASE 又以它为前提。补这条链时，四条性质必须在结构层成立：

1. **裁决不可变**：GateDecision 是"某人某时对某版本做出了某结论"的**事实**。
   本表没有 UPDATE 路径——只授 SELECT/INSERT，并加 append-only 触发器兜住
   超级用户与迁移角色（与 qc_reports / delivery_manifests 同口径）。
   修正裁决 = 新建一条，轨迹自动完整。
2. **Agent 不得作为决策人**：授权层按 risk_level=CRITICAL 拦一次，本表再用
   CHECK 拦一次。授权层拦的是"发起请求的主体"，落库的是"被记为决策人的主体"，
   两者必须同时成立——否则超级用户手工插入一条 Agent 裁决就能绕过全部代码。
3. **缺证据不得 PASS**：`decision = APPROVED` 必须有至少一条 `evidence_refs`
   （CHECK + 领域校验双份）。拒绝/退回不强制证据，理由由 reason_code/comment 承载。
4. **同一输入快照与规则版本下同一结论只记一次**：唯一索引做幂等，
   并发下由约束兜底而不是靠先查后插。不同快照 = 重新评审，允许新增。

为什么必须新建 schema 与角色：
- Gate 是**对人类职责主体的授权裁决**，资产域角色代表"生成/检测流水线"。
  共用身份等于让资产写入路径同时握有裁决写入权——「Agent 不得自批自用」
  在数据库层失去最后一道边界。
- 新增域必须同时做三件事（见 packages/shared/db_roles.py）：登记角色名、
  本迁移建角色并精确授权、纳入分区回收名单（``all_business_roles`` 自动覆盖）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from packages.shared.config import get_settings
from packages.shared.db_roles import DatabaseDomain, role_for

# revision identifiers, used by Alembic.
revision: str = "e8f9a0b1c2d3"
down_revision: str | Sequence[str] | None = "d6e7f8a9b0c1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "qc_governance"
TABLE = "gate_decisions"
OUTBOX_SCHEMA = "outbox"
ALEMBIC_VERSION = "public.alembic_version"

_ORG_SETTING = "app.current_org_id"
_SCOPE_SETTING = "app.current_projects"

#: 契约 7.4 声明的全部裁决取值（迁移是历史快照，不能 import 代码）。
_DECISION_VALUES = ("APPROVED", "REJECTED", "RETURNED", "OVERRIDDEN")

_APPEND_ONLY_GUARD = f"""
CREATE OR REPLACE FUNCTION {SCHEMA}.{TABLE}_guard()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION
        '{TABLE} 是 append-only Gate 裁决事实，禁止 % （裁决不可改写，'
        '修正须新建一条裁决并附新证据）', TG_OP;
END;
$$;
"""


def _scope_expression(column: str) -> str:
    """项目归属判定表达式（与 e7f8a9b0c1d2 的策略文本保持同一口径）。"""
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


def upgrade() -> None:
    role = role_for(DatabaseDomain.QC_GOVERNANCE)

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
        # GateDecision 业务列（契约字段）
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("gate_key", sa.String(32), nullable=False),
        sa.Column("gate_instance_id", sa.String(64), nullable=False),
        sa.Column("decision", sa.String(24), nullable=False),
        sa.Column("constraint_snapshot_id", sa.String(128), nullable=False),
        sa.Column("ruleset_version", sa.Integer(), nullable=False),
        sa.Column("entry_criteria", sa.Text(), nullable=False),
        sa.Column("required_artifacts", sa.Text(), nullable=False),
        sa.Column("reviewers", postgresql.JSONB(), nullable=False),
        sa.Column("reason_code", sa.String(64), nullable=False),
        sa.Column("comment", sa.Text(), nullable=False),
        sa.Column("evidence_refs", postgresql.JSONB(), nullable=False),
        sa.Column("scope", postgresql.JSONB(), nullable=False),
        sa.Column("decided_by", sa.String(64), nullable=False),
        # 内部列（契约无此字段）：把「Agent 不得自批」压到数据层。
        sa.Column("decided_by_subject_type", sa.String(16), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            f"decision IN ({', '.join(repr(v) for v in _DECISION_VALUES)})",
            name="ck_qc_governance_gate_decisions_decision_registered",
        ),
        sa.CheckConstraint(
            "decided_by_subject_type <> 'agent'",
            name="ck_qc_governance_gate_decisions_no_agent_decider",
        ),
        sa.CheckConstraint(
            "decision <> 'APPROVED' OR jsonb_array_length(evidence_refs) > 0",
            name="ck_qc_governance_gate_decisions_approved_needs_evidence",
        ),
        schema=SCHEMA,
    )

    # 索引名与 SQLAlchemy 自动命名一致（EntityMixin 的 tenant_id /
    # organization_id 带 index=True）。名字不一致会让 alembic check 报假漂移。
    op.create_index(
        "ix_qc_governance_gate_decisions_project", TABLE, ["project_id"], schema=SCHEMA
    )
    op.create_index(
        "ix_qc_governance_gate_decisions_instance",
        TABLE,
        ["gate_instance_id"],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_qc_governance_gate_decisions_tenant_id", TABLE, ["tenant_id"], schema=SCHEMA
    )
    op.create_index(
        "ix_qc_governance_gate_decisions_organization_id",
        TABLE,
        ["organization_id"],
        schema=SCHEMA,
    )
    op.create_index(
        "uq_qc_governance_gate_decisions_idempotency",
        TABLE,
        ["gate_instance_id", "constraint_snapshot_id", "ruleset_version", "decision"],
        unique=True,
        schema=SCHEMA,
    )

    # append-only：先建函数与触发器，再做授权——顺序反了会出现"能改已建表"的窗口。
    op.execute(_APPEND_ONLY_GUARD)
    op.execute(
        f"""
        CREATE TRIGGER trg_{TABLE}_guard
        BEFORE UPDATE OR DELETE ON {SCHEMA}.{TABLE}
        FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.{TABLE}_guard()
        """
    )

    # RLS：租户隔离 + 项目范围（限制性），与既有项目级资源表同口径。
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

    # 授权：本域角色可读、可插入；**不给 UPDATE/DELETE**。
    # 追加事件需要 outbox 的 SELECT/INSERT（与其它域一致）。
    op.execute(f"GRANT SELECT, INSERT ON {SCHEMA}.{TABLE} TO {role}")
    op.execute(f"GRANT USAGE ON SCHEMA {OUTBOX_SCHEMA} TO {role}")
    op.execute(f"GRANT SELECT, INSERT ON {OUTBOX_SCHEMA}.outbox_events TO {role}")
    op.execute(f"GRANT SELECT ON {ALEMBIC_VERSION} TO {role}")


def downgrade() -> None:
    role = role_for(DatabaseDomain.QC_GOVERNANCE)
    op.execute(f"REVOKE ALL ON {SCHEMA}.{TABLE} FROM {role}")
    op.execute(f"REVOKE ALL ON {OUTBOX_SCHEMA}.outbox_events FROM {role}")
    op.execute(f"REVOKE ALL ON {ALEMBIC_VERSION} FROM {role}")
    for policy in (f"project_scope_{TABLE}", f"tenant_isolation_{TABLE}"):
        op.execute(f"DROP POLICY IF EXISTS {policy} ON {SCHEMA}.{TABLE}")
    op.execute(f"ALTER TABLE {SCHEMA}.{TABLE} DISABLE ROW LEVEL SECURITY")
    op.execute(f"DROP TRIGGER IF EXISTS trg_{TABLE}_guard ON {SCHEMA}.{TABLE}")
    op.execute(f"DROP FUNCTION IF EXISTS {SCHEMA}.{TABLE}_guard()")
    op.drop_index(
        "uq_qc_governance_gate_decisions_idempotency",
        table_name=TABLE,
        schema=SCHEMA,
    )
    for index in (
        "ix_qc_governance_gate_decisions_organization_id",
        "ix_qc_governance_gate_decisions_tenant_id",
        "ix_qc_governance_gate_decisions_instance",
        "ix_qc_governance_gate_decisions_project",
    ):
        op.drop_index(index, table_name=TABLE, schema=SCHEMA)
    op.drop_table(TABLE, schema=SCHEMA)
    op.execute(f"REVOKE USAGE ON SCHEMA {SCHEMA} FROM {role}")
    op.execute(f"DROP SCHEMA IF EXISTS {SCHEMA}")
    # 角色保留（DROP ROLE 会因其它对象依赖而失败，且重建角色需要重新授权）；
    # 它已无任何对象权限，留着不影响隔离。
