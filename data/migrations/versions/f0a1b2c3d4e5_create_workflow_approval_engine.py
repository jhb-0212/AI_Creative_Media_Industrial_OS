"""通用审批引擎：workflow schema + 角色 + 流程/票据/选票三张表

Revision ID: f0a1b2c3d4e5
Revises: e8f9a0b1c2d3
Create Date: 2026-11-16

设计依据 ADR-R11-G。三条结构性口径：

1. **事实 append-only**：`approval_flows` / `approval_steps` 只授 SELECT/INSERT，
   并加触发器兜住超级用户与迁移角色；票据是状态投影，UPDATE 只授状态相关列。
2. **多主体语义在数据里可执行**：分组与 AND/OR 模式存在 flow 上，选票唯一键
   `(ticket_id, group_name, voter_subject_id)` 保证一组一人一票；Agent 投票被
   CHECK 拒绝（授权层与领域层之外的数据层最后一道）。
3. **隔离**：三张表都带 tenant_id / project_id，RLS 双策略与既有业务表同口径。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from packages.shared.config import get_settings
from packages.shared.db_roles import DatabaseDomain, role_for

revision: str = "f0a1b2c3d4e5"
down_revision: str | Sequence[str] | None = "e8f9a0b1c2d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "workflow"
OUTBOX_SCHEMA = "outbox"
ALEMBIC_VERSION = "public.alembic_version"

_DECISION_VALUES = ("APPROVE", "REJECT")
_MODE_VALUES = ("ALL", "ANY")
_STATUS_VALUES = ("PENDING", "APPROVED", "REJECTED", "CANCELLED")


def _scope_expression(column: str) -> str:
    return f"""(
        organization_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid
        AND (
            current_setting('app.current_projects', true) = '*'
            OR {column} = ANY (
                string_to_array(
                    NULLIF(current_setting('app.current_projects', true), ''), ','
                )
            )
        )
    )"""


def _append_only_guard(table: str) -> str:
    return f"""
    CREATE OR REPLACE FUNCTION {SCHEMA}.{table}_guard()
    RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        RAISE EXCEPTION
            '{table} 是 append-only 审批事实，禁止 %（修正须追加新事实）', TG_OP;
    END;
    $$;
    """


def _create_role(role: str) -> None:
    password = get_settings().postgres_app_password.get_secret_value()
    if not password:
        raise RuntimeError("POSTGRES_APP_PASSWORD 未配置：拒绝以空口令创建业务域角色")
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


def _enable_rls(table: str, *, project_column: str) -> None:
    op.execute(f"ALTER TABLE {SCHEMA}.{table} ENABLE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY tenant_isolation_{table} ON {SCHEMA}.{table}
        USING (
            tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid
        )
        """
    )
    expression = _scope_expression(project_column)
    op.execute(
        f"""
        CREATE POLICY project_scope_{table} ON {SCHEMA}.{table}
        AS RESTRICTIVE FOR ALL USING ({expression}) WITH CHECK ({expression})
        """
    )


def _envelope_columns() -> list[sa.Column]:
    return [
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
    ]


def upgrade() -> None:
    role = role_for(DatabaseDomain.WORKFLOW)
    op.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
    _create_role(role)
    op.execute(f"GRANT USAGE ON SCHEMA {SCHEMA} TO {role}")

    op.create_table(
        "approval_flows",
        *_envelope_columns(),
        sa.Column("action_key", sa.String(96), nullable=False),
        sa.Column("resource_type", sa.String(96), nullable=False),
        sa.Column("resource_id", sa.String(96), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("mode", sa.String(8), nullable=False),
        sa.Column("groups", postgresql.JSONB(), nullable=False),
        sa.Column("ruleset_version", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            f"mode IN ({', '.join(repr(v) for v in _MODE_VALUES)})",
            name="ck_workflow_approval_flows_mode_registered",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_workflow_approval_flows_action_key",
        "approval_flows",
        ["action_key"],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_workflow_approval_flows_resource",
        "approval_flows",
        ["resource_type", "resource_id"],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_workflow_approval_flows_tenant_id",
        "approval_flows",
        ["tenant_id"],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_workflow_approval_flows_organization_id",
        "approval_flows",
        ["organization_id"],
        schema=SCHEMA,
    )

    op.create_table(
        "approval_tickets",
        *_envelope_columns(),
        sa.Column("flow_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            f"status IN ({', '.join(repr(v) for v in _STATUS_VALUES)})",
            name="ck_workflow_approval_tickets_status_registered",
        ),
        sa.CheckConstraint(
            "status = 'PENDING' OR completed_at IS NOT NULL",
            name="ck_workflow_approval_tickets_terminal_has_time",
        ),
        schema=SCHEMA,
    )
    for name, columns in (
        ("ix_workflow_approval_tickets_flow_id", ["flow_id"]),
        ("ix_workflow_approval_tickets_project", ["project_id"]),
        ("ix_workflow_approval_tickets_tenant_id", ["tenant_id"]),
        ("ix_workflow_approval_tickets_organization_id", ["organization_id"]),
    ):
        op.create_index(name, "approval_tickets", columns, schema=SCHEMA)

    op.create_table(
        "approval_steps",
        *_envelope_columns(),
        sa.Column("ticket_id", sa.String(64), nullable=False),
        sa.Column("group_name", sa.String(64), nullable=False),
        sa.Column("voter_subject_id", sa.String(64), nullable=False),
        sa.Column("voter_subject_type", sa.String(16), nullable=False),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.UniqueConstraint(
            "ticket_id",
            "group_name",
            "voter_subject_id",
            name="uq_workflow_approval_steps_one_vote_per_group",
        ),
        sa.CheckConstraint(
            f"decision IN ({', '.join(repr(v) for v in _DECISION_VALUES)})",
            name="ck_workflow_approval_steps_decision_registered",
        ),
        sa.CheckConstraint(
            "voter_subject_type <> 'agent'",
            name="ck_workflow_approval_steps_no_agent_voter",
        ),
        sa.CheckConstraint(
            "decision <> 'REJECT' OR length(btrim(reason)) > 0",
            name="ck_workflow_approval_steps_reject_needs_reason",
        ),
        schema=SCHEMA,
    )
    for name, columns in (
        ("ix_workflow_approval_steps_ticket_id", ["ticket_id"]),
        ("ix_workflow_approval_steps_tenant_id", ["tenant_id"]),
        ("ix_workflow_approval_steps_organization_id", ["organization_id"]),
    ):
        op.create_index(name, "approval_steps", columns, schema=SCHEMA)

    # append-only 触发器先建，再授权，避免"能改已建表"的窗口。
    for table in ("approval_flows", "approval_steps"):
        op.execute(_append_only_guard(table))
        op.execute(
            f"""
            CREATE TRIGGER trg_{table}_guard
            BEFORE UPDATE OR DELETE ON {SCHEMA}.{table}
            FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.{table}_guard()
            """
        )

    _enable_rls("approval_flows", project_column="project_id")
    _enable_rls("approval_tickets", project_column="project_id")
    _enable_rls("approval_steps", project_column="project_id")

    op.execute(f"GRANT SELECT, INSERT ON {SCHEMA}.approval_flows TO {role}")
    op.execute(f"GRANT SELECT, INSERT ON {SCHEMA}.approval_steps TO {role}")
    # 票据只开放状态机需要的列；其余信封/事实列不可改（与 outbox 投递列同口径）。
    op.execute(
        f"""
        GRANT SELECT, INSERT,
              UPDATE (status, version, completed_at, updated_at, audit_ref)
        ON {SCHEMA}.approval_tickets TO {role}
        """
    )
    op.execute(f"GRANT USAGE ON SCHEMA {OUTBOX_SCHEMA} TO {role}")
    op.execute(f"GRANT SELECT, INSERT ON {OUTBOX_SCHEMA}.outbox_events TO {role}")
    op.execute(f"GRANT SELECT ON {ALEMBIC_VERSION} TO {role}")


def downgrade() -> None:
    role = role_for(DatabaseDomain.WORKFLOW)
    for table in ("approval_steps", "approval_tickets", "approval_flows"):
        op.execute(f"REVOKE ALL ON {SCHEMA}.{table} FROM {role}")
        for policy in (f"project_scope_{table}", f"tenant_isolation_{table}"):
            op.execute(f"DROP POLICY IF EXISTS {policy} ON {SCHEMA}.{table}")
        op.execute(f"ALTER TABLE {SCHEMA}.{table} DISABLE ROW LEVEL SECURITY")
    for table in ("approval_steps", "approval_flows"):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_guard ON {SCHEMA}.{table}")
        op.execute(f"DROP FUNCTION IF EXISTS {SCHEMA}.{table}_guard()")
    for table in ("approval_steps", "approval_tickets", "approval_flows"):
        op.drop_table(table, schema=SCHEMA)
    op.execute(f"REVOKE ALL ON {OUTBOX_SCHEMA}.outbox_events FROM {role}")
    op.execute(f"REVOKE ALL ON {ALEMBIC_VERSION} FROM {role}")
    op.execute(f"REVOKE USAGE ON SCHEMA {SCHEMA} FROM {role}")
    op.execute(f"DROP SCHEMA IF EXISTS {SCHEMA}")
