"""QC 待检队列落库：qc_governance.qc_tasks + RLS + 授权（Phase 3）。

为什么需要这张表
- QC.REPORT.CREATE 只是「登记报告」的入口，不记录「哪份产物还没检」。没有队列，
  漏检只能靠人记得，漏了不会留下任何缺口——这是「有动作、没有流程」的典型债。
- 队列必须与生成任务同构地持租约：QC Worker 崩溃后，靠 lease_expires_at 才能
  把任务退回 QUEUED，而不是永远卡在 RUNNING 等人工发现。

库层不变量：
1. 信封列与 C.3.1 逐列一致；tenant_id 参与 RLS。
2. status 受控（CHECK 由 QCTaskStatus 派生）——写 'PASSED' 之外的大小写变体直接拒绝。
3. generation_task_id 唯一：同一生成任务只能入队一次（重复入队 = 同一产物检两遍）。
4. qc_report_id 不做物理外键：报告归 asset_service 域，跨域外键会把两个 schema
   的迁移顺序绑死；判定必须有报告这一点由领域层与事件 payload 保证。

Revision ID: f1a2b3c4d5e6
Revises: e6f7a8b9c0d1
Create Date: 2026-11-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from packages.shared.db_grants import QC_GOVERNANCE, converge_statements
from packages.shared.db_roles import domain_role
from packages.shared.rls import tenant_scope_predicate
from packages.shared.status_constraint import status_check
from services.project_planning.domain.enums import QCTaskStatus

revision: str = "f1a2b3c4d5e6"
down_revision: str | Sequence[str] | None = "e6f7a8b9c0d1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCHEMA = QC_GOVERNANCE
_TABLE = "qc_tasks"
_TABLES = (_TABLE,)


def _envelope_columns() -> list[sa.schema.Column]:
    """C.3.1 信封列（迁移是时间胶囊，逐列写死）。"""
    return [
        sa.Column("entity_id", sa.String(64), primary_key=True),
        sa.Column("entity_type", sa.String(64), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("entity_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("owner_module", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.String(64), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("audit_ref", sa.String(128), nullable=True),
    ]


def _project_visibility() -> str:
    return (
        "USING ("
        f"{tenant_scope_predicate()} AND "
        "EXISTS (SELECT 1 FROM project_planning.projects p "
        f"WHERE p.entity_id = {_SCHEMA}.{_TABLE}.project_id "
        f"AND p.tenant_id = {_SCHEMA}.{_TABLE}.tenant_id)"
        ")"
    )


def _create_table() -> None:
    op.create_table(
        _TABLE,
        *_envelope_columns(),
        sa.Column("qc_task_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("shot_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("asset_version_id", sa.String(64), nullable=False),
        sa.Column("generation_task_id", sa.String(64), nullable=False),
        sa.Column("quality_profile_version", sa.String(64), nullable=False),
        sa.Column("lease_owner", sa.String(128), nullable=False, server_default=""),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("qc_report_id", sa.String(64), nullable=False, server_default=""),
        status_check((s.value for s in QCTaskStatus), table=_TABLE),
        schema=_SCHEMA,
    )


def _create_indexes() -> None:
    op.create_index(
        op.f(f"ix_qc_governance_{_TABLE}_tenant_id"),
        _TABLE,
        ["tenant_id"],
        schema=_SCHEMA,
    )
    op.create_index(
        op.f(f"ix_qc_governance_{_TABLE}_status"),
        _TABLE,
        ["status"],
        schema=_SCHEMA,
    )
    # 唯一：同一生成任务只能对应一个 QC 任务。
    op.create_index(
        op.f(f"uq_qc_governance_{_TABLE}_generation_task"),
        _TABLE,
        ["generation_task_id"],
        unique=True,
        schema=_SCHEMA,
    )


def _enable_rls() -> None:
    op.execute(f"ALTER TABLE {_SCHEMA}.{_TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation_{_TABLE} ON {_SCHEMA}.{_TABLE} {_project_visibility()}"
    )
    op.execute(f"ALTER TABLE {_SCHEMA}.{_TABLE} FORCE ROW LEVEL SECURITY")


def upgrade() -> None:
    _create_table()
    _create_indexes()
    _enable_rls()
    role = domain_role(_SCHEMA)
    for statement in converge_statements(role, _SCHEMA, tables=set(_TABLES)):
        op.execute(statement)


def downgrade() -> None:
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation_{_TABLE} ON {_SCHEMA}.{_TABLE}")
    op.execute(f"ALTER TABLE {_SCHEMA}.{_TABLE} DISABLE ROW LEVEL SECURITY")
    op.drop_index(
        op.f(f"uq_qc_governance_{_TABLE}_generation_task"),
        table_name=_TABLE,
        schema=_SCHEMA,
    )
    op.drop_index(op.f(f"ix_qc_governance_{_TABLE}_status"), table_name=_TABLE, schema=_SCHEMA)
    op.drop_index(op.f(f"ix_qc_governance_{_TABLE}_tenant_id"), table_name=_TABLE, schema=_SCHEMA)
    op.drop_table(_TABLE, schema=_SCHEMA)
