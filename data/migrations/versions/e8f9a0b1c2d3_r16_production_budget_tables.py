"""R16 生产链与预算对象运行时落地：建表 + RLS + 授权（过渡期 project_planning schema）

建四张表：
- production_routes / generation_tasks / shot_budget_requests / budget_ledger

为什么落在 project_planning schema（过渡口径）：
- 本机无版本控制，一次扩建 3 个新域（schema + role + lifespan 双池 + readiness）
  的爆炸半径过大；先把四张表落在既有 project_planning 域，与 ConstraintPack
  同角色同 RLS 口径，使「持久化」先可运行、可校验。
- 独立域拆分登记在 ADR，Phase 2/3 执行。

库层不变量：
1. 信封列与 C.3.1 逐列一致；tenant_id 参与 RLS。
2. 项目作用域三表（routes/tasks/budget_requests）用父项目存在性表达组织可见性；
   budget_ledger 无 project_id，按 tenant 维度隔离。
3. budget_ledger 的 idempotency_key 唯一——同一审批重复追加不得产生两条流水。
4. status 受控（status_check 由领域枚举生成）。

Revision ID: e8f9a0b1c2d3
Revises: d7e8f9a0b1c2
Create Date: 2026-11-22
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from packages.shared.db_grants import PROJECT_PLANNING, converge_statements
from packages.shared.db_roles import domain_role
from packages.shared.rls import tenant_scope_predicate
from packages.shared.status_constraint import status_check
from services.project_planning.domain.enums import (
    GenerationTaskStatus,
    NoLifecycleStatus,
    ProductionRouteStatus,
    ShotBudgetRequestStatus,
)

revision: str = "e8f9a0b1c2d3"
down_revision: str | Sequence[str] | None = "d7e8f9a0b1c2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCHEMA = PROJECT_PLANNING

_ROUTES = "production_routes"
_TASKS = "generation_tasks"
_REQUESTS = "shot_budget_requests"
_LEDGER = "budget_ledger"
_TABLES = (_ROUTES, _TASKS, _REQUESTS, _LEDGER)


def _envelope_columns() -> list[sa.schema.Column]:
    """C.3.1 信封列（迁移是时间胶囊，逐列写死，不抽公共函数）。"""
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


def _project_visibility(table: str) -> str:
    """项目作用域可见性：租户 + 父项目存在（子表不冗余 organization_id）。"""
    return (
        "USING ("
        f"{tenant_scope_predicate()} AND "
        "EXISTS (SELECT 1 FROM project_planning.projects p "
        f"WHERE p.entity_id = {_SCHEMA}.{table}.project_id "
        f"AND p.tenant_id = {_SCHEMA}.{table}.tenant_id)"
        ")"
    )


def _tenant_visibility() -> str:
    """仅租户维度（账本流水无项目作用域）。"""
    return f"USING ({tenant_scope_predicate()})"


def _create_tables() -> None:
    op.create_table(
        _ROUTES,
        *_envelope_columns(),
        sa.Column("route_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("shot_id", sa.String(64), nullable=False),
        sa.Column("task_ref", sa.String(64), nullable=False),
        sa.Column("route_type", sa.String(32), nullable=False),
        sa.Column("capability_ref", sa.String(200), nullable=False),
        sa.Column("basis_ref", sa.String(200), nullable=False),
        sa.UniqueConstraint("project_id", "entity_id", name="uq_production_routes_entity_project"),
        status_check((s.value for s in ProductionRouteStatus), table=_ROUTES),
        schema=_SCHEMA,
    )

    op.create_table(
        _TASKS,
        *_envelope_columns(),
        sa.Column("generation_task_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("shot_id", sa.String(64), nullable=False),
        sa.Column("shot_revision_id", sa.String(64), nullable=False),
        sa.Column("production_route_ref", sa.String(64), nullable=False),
        sa.Column("model_adapter_ref", sa.String(128), nullable=False),
        sa.Column("constraint_snapshot_id", sa.String(128), nullable=False),
        sa.Column("budget_ref", sa.String(128), nullable=False),
        sa.Column("prompt_ref", sa.String(200), nullable=False),
        sa.UniqueConstraint("project_id", "entity_id", name="uq_generation_tasks_entity_project"),
        status_check((s.value for s in GenerationTaskStatus), table=_TASKS),
        schema=_SCHEMA,
    )

    op.create_table(
        _REQUESTS,
        *_envelope_columns(),
        sa.Column("request_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("shot_id", sa.String(64), nullable=False),
        sa.Column("account_type", sa.String(32), nullable=False),
        sa.Column("purpose", sa.String(128), nullable=False),
        sa.Column("cost_category", sa.String(64), nullable=False),
        sa.Column("amount_limit", sa.Integer(), nullable=False),
        sa.Column("requested_by", sa.String(64), nullable=False),
        sa.Column("approver", sa.String(64), nullable=False, server_default=""),
        sa.Column("decision_reason", sa.String(200), nullable=False, server_default=""),
        sa.CheckConstraint("amount_limit >= 0", name="ck_shot_budget_requests_amount_nonneg"),
        status_check((s.value for s in ShotBudgetRequestStatus), table=_REQUESTS),
        schema=_SCHEMA,
    )

    op.create_table(
        _LEDGER,
        *_envelope_columns(),
        sa.Column("transaction_id", sa.String(64), nullable=False),
        sa.Column("account_type", sa.String(32), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(8), nullable=False, server_default="CNY"),
        sa.Column("transaction_kind", sa.String(32), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("approval_ref", sa.String(128), nullable=False),
        sa.Column("source", sa.String(128), nullable=False),
        sa.UniqueConstraint("idempotency_key", name="uq_budget_ledger_idempotency_key"),
        status_check((s.value for s in NoLifecycleStatus), table=_LEDGER),
        schema=_SCHEMA,
    )


def _create_indexes() -> None:
    for table in _TABLES:
        op.create_index(
            op.f(f"ix_project_planning_{table}_tenant_id"),
            table,
            ["tenant_id"],
            schema=_SCHEMA,
        )
    for table in (_ROUTES, _TASKS, _REQUESTS):
        op.create_index(
            op.f(f"ix_project_planning_{table}_project_id"),
            table,
            ["project_id"],
            schema=_SCHEMA,
        )


def _enable_rls() -> None:
    for table in _TABLES:
        op.execute(f"ALTER TABLE {_SCHEMA}.{table} ENABLE ROW LEVEL SECURITY")
        predicate = _tenant_visibility() if table == _LEDGER else _project_visibility(table)
        op.execute(f"CREATE POLICY tenant_isolation_{table} ON {_SCHEMA}.{table} {predicate}")
        op.execute(f"ALTER TABLE {_SCHEMA}.{table} FORCE ROW LEVEL SECURITY")


def upgrade() -> None:
    _create_tables()
    _create_indexes()
    _enable_rls()
    role = domain_role(_SCHEMA)
    for statement in converge_statements(role, _SCHEMA, tables=set(_TABLES)):
        op.execute(statement)


def downgrade() -> None:
    for table in _TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation_{table} ON {_SCHEMA}.{table}")
        op.execute(f"ALTER TABLE {_SCHEMA}.{table} DISABLE ROW LEVEL SECURITY")
    for table in (_ROUTES, _TASKS, _REQUESTS):
        op.drop_index(
            op.f(f"ix_project_planning_{table}_project_id"),
            table_name=table,
            schema=_SCHEMA,
        )
    for table in _TABLES:
        op.drop_index(
            op.f(f"ix_project_planning_{table}_tenant_id"),
            table_name=table,
            schema=_SCHEMA,
        )
    for table in (_LEDGER, _REQUESTS, _TASKS, _ROUTES):
        op.drop_table(table, schema=_SCHEMA)
