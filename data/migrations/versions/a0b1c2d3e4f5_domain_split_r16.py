"""R16 三域拆分 cutover：五表出 project_planning，进各自 schema + 各自角色

为什么一次性做完（而不是分域分批）
--------------------------------
五张表今天分别由四条路由使用，但它们在库里同处 project_planning schema、同一
个 ``project_planning_role``。只要还有一张留在旧 schema，「宽角色」就必须保留
对它的写权限，拆分就没有收窄任何东西——所以分批只会得到两个中间态，且两个
中间态的权限面都是错的。本迁移因此是**一次 cutover**。

搬什么（纯结构性搬移，五表在 cutover 时点均为 0 行）
-------------------------------------------------
- ``production_routes`` / ``generation_tasks`` → ``production_planning``
- ``shot_budget_requests`` / ``budget_ledger``   → ``budget_quota``
- ``gate_decisions``                            → ``qc_governance``

库层不变量（逐条都可被独立验证）
1. 三个新 schema 各一个专用角色（``CREATE ROLE … LOGIN NOSUPERUSER NOCREATEDB
   NOCREATEROLE``），角色名只从配置读（``db_roles``），迁移不写字面量；
2. 信封列与 C.3.1 逐列一致；``tenant_id`` 参与 RLS，项目作用域表经父项目
   存在性表达组织可见性（子表不冗余 organization_id）；
3. 授权**只经** ``db_grants`` 矩阵生成：账本与 Gate 决策只有 SELECT/INSERT；
4. 新角色对 ``project_planning.projects`` 只有三列只读（列级授权），因此
   「生产域顺手读剧本」在数据库层被拒绝；
5. 旧 schema 的五张表在搬移完成后 DROP——不保留新旧两套（双份事实源比缺权限
   更难发现）。

Revision ID: a0b1c2d3e4f5
Revises: f9a0b1c2d3e4
Create Date: 2026-11-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

from packages.shared.db_grants import (
    BUDGET_QUOTA,
    PRODUCTION_PLANNING,
    QC_GOVERNANCE,
    converge_statements,
)
from packages.shared.db_roles import domain_role
from packages.shared.rls import tenant_scope_predicate
from packages.shared.status_constraint import status_check
from services.project_planning.domain.enums import (
    GenerationTaskStatus,
    NoLifecycleStatus,
    ProductionRouteStatus,
    ShotBudgetRequestStatus,
)

revision: str = "a0b1c2d3e4f5"
down_revision: str | Sequence[str] | None = "f9a0b1c2d3e4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_SCHEMA = "project_planning"

_ROUTES = "production_routes"
_TASKS = "generation_tasks"
_REQUESTS = "shot_budget_requests"
_LEDGER = "budget_ledger"
_GATE = "gate_decisions"

# 域 → 本域自己的表（授权快照按域切，不用「当前矩阵」代替历史快照）。
_DOMAIN_TABLES: dict[str, tuple[str, ...]] = {
    PRODUCTION_PLANNING: (_ROUTES, _TASKS),
    BUDGET_QUOTA: (_REQUESTS, _LEDGER),
    QC_GOVERNANCE: (_GATE,),
}
_PROJECT_SCOPED = (_ROUTES, _TASKS, _REQUESTS, _GATE)
_TABLES = (_ROUTES, _TASKS, _REQUESTS, _LEDGER, _GATE)

# 本迁移负责收敛的全部表名（域内 + 平台共享 + 跨域列级只读的目标）。
# converge_statements 的 tables= 是**冻结范围**参数（只按表名过滤），必须把
# 共享表一起写进来，否则新角色拿不到 audit/outbox 写权限，第一次写事件就
# permission denied（那是运行时故障，静态判据看不见）。
_GRANT_SCOPE: frozenset[str] = frozenset(
    {
        *_TABLES,
        "status_transitions",
        "outbox_events",
        "revoked_tokens",
        "alembic_version",
        "projects",
    }
)


def _envelope_columns() -> list[sa.schema.SchemaItem]:
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


def _create_role_if_absent(role: str, password: str) -> None:
    """幂等建角色：已存在则只更新口令（重复执行迁移不报错）。

    为什么不用 ``CREATE ROLE … IF NOT EXISTS``：PG 没有这个语法（它是
    ``CREATE TABLE`` 才有的），照抄会得到 syntax error。
    """
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
                CREATE ROLE {role} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
                    PASSWORD '{password}';
            ELSE
                ALTER ROLE {role} WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
                    PASSWORD '{password}';
            END IF;
        END $$;
        """
    )


def _table_ddl(table: str) -> list[sa.schema.SchemaItem]:
    """按表给聚合根列（与 ORM 逐列一致，alembic check 保证无漂移）。"""
    if table == _ROUTES:
        return [
            sa.Column("route_id", sa.String(64), nullable=False),
            sa.Column("project_id", sa.String(64), nullable=False),
            sa.Column("shot_id", sa.String(64), nullable=False),
            sa.Column("task_ref", sa.String(64), nullable=False),
            sa.Column("route_type", sa.String(32), nullable=False),
            sa.Column("capability_ref", sa.String(200), nullable=False),
            sa.Column("basis_ref", sa.String(200), nullable=False),
            sa.UniqueConstraint(
                "project_id", "entity_id", name="uq_production_routes_entity_project"
            ),
            status_check(
                (s.value for s in ProductionRouteStatus), table=_ROUTES
            ),
        ]
    if table == _TASKS:
        return [
            sa.Column("generation_task_id", sa.String(64), nullable=False),
            sa.Column("project_id", sa.String(64), nullable=False),
            sa.Column("shot_id", sa.String(64), nullable=False),
            sa.Column("shot_revision_id", sa.String(64), nullable=False),
            sa.Column("production_route_ref", sa.String(64), nullable=False),
            sa.Column("model_adapter_ref", sa.String(128), nullable=False),
            sa.Column("constraint_snapshot_id", sa.String(128), nullable=False),
            sa.Column("budget_ref", sa.String(128), nullable=False),
            sa.Column("prompt_ref", sa.String(200), nullable=False),
            sa.UniqueConstraint(
                "project_id", "entity_id", name="uq_generation_tasks_entity_project"
            ),
            status_check((s.value for s in GenerationTaskStatus), table=_TASKS),
        ]
    if table == _REQUESTS:
        return [
            sa.Column("request_id", sa.String(64), nullable=False),
            sa.Column("project_id", sa.String(64), nullable=False),
            sa.Column("shot_id", sa.String(64), nullable=False),
            sa.Column("account_type", sa.String(32), nullable=False),
            sa.Column("purpose", sa.String(128), nullable=False),
            sa.Column("cost_category", sa.String(64), nullable=False),
            sa.Column("amount_limit", sa.Integer(), nullable=False),
            sa.Column("requested_by", sa.String(64), nullable=False),
            sa.Column("approver", sa.String(64), nullable=False, server_default=""),
            sa.Column(
                "decision_reason", sa.String(200), nullable=False, server_default=""
            ),
            sa.CheckConstraint(
                "amount_limit >= 0", name="ck_shot_budget_requests_amount_nonneg"
            ),
            status_check((s.value for s in ShotBudgetRequestStatus), table=_REQUESTS),
        ]
    if table == _LEDGER:
        return [
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
        ]
    return [
        sa.Column("gate_key", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("decided_by", sa.String(64), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("gate_instance_id", sa.String(64), nullable=False, server_default=""),
        sa.Column(
            "constraint_snapshot_id", sa.String(128), nullable=False, server_default=""
        ),
        sa.Column("entry_criteria", sa.String(500), nullable=False, server_default=""),
        sa.Column("reason_code", sa.String(64), nullable=False, server_default=""),
        sa.Column("comment", sa.String(500), nullable=False, server_default=""),
        sa.Column("required_artifacts", sa.String(500), nullable=False, server_default=""),
        sa.Column("evidence_refs", JSONB, nullable=False, server_default="[]"),
        sa.Column("reviewers", JSONB, nullable=False, server_default="[]"),
        sa.Column("ruleset_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("scope", JSONB, nullable=False, server_default="{}"),
        sa.CheckConstraint(
            "decision IN ('APPROVED', 'REJECTED')",
            name="ck_gate_decisions_decision_controlled",
        ),
        status_check((s.value for s in NoLifecycleStatus), table=_GATE),
    ]


def _index_columns(table: str) -> list[list[str]]:
    """索引列清单（索引名按 schema 命名，避免同名索引跨 schema 混淆排障）。"""
    if table == _GATE:
        return [["tenant_id"], ["project_id"], ["project_id", "gate_key"]]
    if table == _LEDGER:
        return [["tenant_id"]]
    return [["tenant_id"], ["project_id"]]


def _index_name(schema: str, table: str, columns: Sequence[str]) -> str:
    suffix = "_".join(columns)
    return f"ix_{schema}_{table}_{suffix}"


def _create_tables(schema: str, tables: Sequence[str]) -> None:
    for table in tables:
        op.create_table(
            table,
            *_envelope_columns(),
            *_table_ddl(table),
            schema=schema,
        )


def _create_indexes(schema: str, tables: Sequence[str]) -> None:
    for table in tables:
        for columns in _index_columns(table):
            op.create_index(
                op.f(_index_name(schema, table, columns)),
                table,
                list(columns),
                schema=schema,
            )


def _visibility(schema: str, table: str) -> str:
    """组织可见性：租户谓词 + 父项目存在性（账本无项目作用域，仅租户）。"""
    base = f"USING ({tenant_scope_predicate()}"
    if table == _LEDGER:
        return f"{base})"
    return (
        f"{base} AND EXISTS (SELECT 1 FROM project_planning.projects p "
        f"WHERE p.entity_id = {schema}.{table}.project_id "
        f"AND p.tenant_id = {schema}.{table}.tenant_id))"
    )


def _enable_rls(schema: str, tables: Sequence[str]) -> None:
    for table in tables:
        op.execute(f"ALTER TABLE {schema}.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation_{table} ON {schema}.{table} "
            f"{_visibility(schema, table)}"
        )
        op.execute(f"ALTER TABLE {schema}.{table} FORCE ROW LEVEL SECURITY")


def _drop_rls(schema: str, tables: Sequence[str]) -> None:
    for table in tables:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation_{table} ON {schema}.{table}")
        op.execute(f"ALTER TABLE {schema}.{table} DISABLE ROW LEVEL SECURITY")


def upgrade() -> None:
    # 1) schema + 角色（角色名从配置读，口令从同一配置上下文取）
    from packages.shared.config import get_settings

    settings = get_settings()
    role_passwords = {
        PRODUCTION_PLANNING: settings.postgres_production_planning_password.get_secret_value(),
        BUDGET_QUOTA: settings.postgres_budget_quota_password.get_secret_value(),
        QC_GOVERNANCE: settings.postgres_qc_governance_password.get_secret_value(),
    }
    for domain in (PRODUCTION_PLANNING, BUDGET_QUOTA, QC_GOVERNANCE):
        op.execute(f"CREATE SCHEMA IF NOT EXISTS {domain}")
        # 角色必须先存在：给不存在的角色授权会直接失败，把升级路径堵死。
        _create_role_if_absent(domain_role(domain), role_passwords[domain])
    # 注：schema USAGE 与全部表级授权都经 converge_statements 生成（第 4 步），
    # 迁移里不写任何 GRANT 字面量——「迁移是矩阵的执行器」这条不变量才成立。

    # 3) 新 schema 内建表 + 索引 + RLS
    for domain, tables in _DOMAIN_TABLES.items():
        _create_tables(domain, tables)
        _create_indexes(domain, tables)
        _enable_rls(domain, tables)

    # 4) 授权只经矩阵：每域一次收敛（含平台共享表与跨域列级只读）
    for domain in (PRODUCTION_PLANNING, BUDGET_QUOTA, QC_GOVERNANCE):
        for statement in converge_statements(
            domain_role(domain), domain, tables=_GRANT_SCOPE
        ):
            op.execute(statement)

    # 5) 旧表退出：不保留双份事实源（cutover 时点五表均为 0 行，无数据搬运）
    _drop_rls(_OLD_SCHEMA, _TABLES)
    for table in (_LEDGER, _REQUESTS, _TASKS, _ROUTES, _GATE):
        for columns in _index_columns(table):
            op.execute(
                f"DROP INDEX IF EXISTS {_OLD_SCHEMA}."
                f"{_index_name(_OLD_SCHEMA, table, columns)}"
            )
        op.execute(f"DROP TABLE IF EXISTS {_OLD_SCHEMA}.{table}")

    # 6) 旧角色的授权随表 DROP 一并消失——PG 的表级 ACL 挂在对象上，对象没了
    #    就没有再 REVOKE 的必要；对已 DROP 的表 REVOKE 会直接报 relation does
    #    not exist（第一次执行就撞上了，整条迁移回滚）。


def downgrade() -> None:
    """回滚到「五表在 project_planning、由 project_planning_role 读写」的旧形态。

    这是**结构性回滚**：cutover 后写入新 schema 的行不会回来（本迁移不回填），
    回滚只重建表结构与旧授权口径。写明这一点比写一个「看起来能回滚」的实现
    更安全——误以为能拿回数据才是真正的风险。

    旧授权口径逐条写出（不用 ``converge_statements``）：矩阵在 cutover 之后
    已不再声明这五张表，拿它生成语句会得到「什么都不授」，旧角色立刻失去
    全部权限——降级路径本身成了故障。
    """
    _drop_rls(BUDGET_QUOTA, (_REQUESTS, _LEDGER))
    _drop_rls(QC_GOVERNANCE, (_GATE,))
    _drop_rls(PRODUCTION_PLANNING, (_ROUTES, _TASKS))
    old_role = domain_role(_OLD_SCHEMA)

    # 旧形态：五表回 project_planning（索引名沿用旧前缀）
    for table in (_ROUTES, _TASKS, _REQUESTS, _LEDGER, _GATE):
        op.create_table(
            table,
            *_envelope_columns(),
            *_table_ddl(table),
            schema=_OLD_SCHEMA,
        )
        for columns in _index_columns(table):
            op.create_index(
                op.f(_index_name(_OLD_SCHEMA, table, columns)),
                table,
                list(columns),
                schema=_OLD_SCHEMA,
            )
    _enable_rls(_OLD_SCHEMA, _TABLES)
    from packages.shared.db_grants import legacy_r16_grant_statements

    for statement in legacy_r16_grant_statements(old_role):
        op.execute(statement)

    # 新 schema 的表与角色授权退出（角色本身保留：可能与其它部署共用）
    for domain, tables in _DOMAIN_TABLES.items():
        for table in tables:
            for columns in _index_columns(table):
                op.execute(
                    f"DROP INDEX IF EXISTS {domain}.{_index_name(domain, table, columns)}"
                )
            op.execute(f"DROP TABLE IF EXISTS {domain}.{table}")
    # 角色与 schema USAGE 保留：角色可能被其它部署共用，删角色是不可逆的
    # 共享状态改动，而降级本身只承诺「结构回到 cutover 前」。
