"""QC 门禁整改：租约代次 + 同事务调度投影（WP-01 / WP-02 的库层）。

为什么要有 ``lease_generation``
-------------------------------
``entity_version`` 是**实体版本**（乐观锁用），``attempt_count`` 是**重试次数**
（重试策略用）。两者都不表达「现在这张租约属于第几代」：租约过期后被别人重领，
``entity_version`` 会涨，但一个旧 Worker 只要重新读一遍就能拿到新版本，从而「合法」
地写出一条属于别人的迟到结果。代次是唯一能把「我是这一代」与「我是这个任务」
分开的标识，因此必须是独立列，不能借用上面任何一个。

为什么投影放独立 schema
----------------------
``qc_governance.qc_tasks`` 是事实源、由 ``qc_governance_role`` 持有；
``task_projection`` 是**调度派生视图**，将来只应由调度身份读。放在同一 schema，
「只授调度读投影」这件事在 PG 里做不到——schema 的 USAGE 会把同 schema 的业务表
一起暴露。独立 schema 让这条边界在库层成立。

投影**不是**第二事实源：任何差异都以 ``qc_tasks`` 为准，可整表重建。

Revision ID: a8b9c0d1e2f3
Revises: f9b0c1d2e3f4
Create Date: 2026-11-26
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from packages.shared.db_grants import QC_GOVERNANCE, converge_statements
from packages.shared.db_roles import domain_role
from packages.shared.rls import tenant_scope_predicate

revision: str = "a8b9c0d1e2f3"
down_revision: str | Sequence[str] | None = "f9b0c1d2e3f4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_QC_SCHEMA = "qc_governance"
_QC_TASKS = "qc_tasks"
_PROJECTION_SCHEMA = "qc_maintenance"
_PROJECTION = "task_projection"
_LEASE_GENERATION_CHECK = (
    "ck_qc_governance_qc_tasks_lease_generation_non_negative"
)


def _add_lease_generation() -> None:
    """给 qc_tasks 加租约代次列。

    用 ``NOT VALID`` + ``VALIDATE`` 两步而不是一条 ``ADD CONSTRAINT``：
    直接加约束会在加锁期间扫全表并校验所有行，表越大持锁越久；拆成两步后
    第一步只拿短暂的 ACCESS EXCLUSIVE 改元数据，第二步的校验在更弱的锁下进行。
    ``DEFAULT 0`` 是 PG11+ 的元数据默认值，不重写表。
    """
    op.execute(
        f"ALTER TABLE {_QC_SCHEMA}.{_QC_TASKS} "
        "ADD COLUMN lease_generation BIGINT NOT NULL DEFAULT 0"
    )
    op.execute(
        f"ALTER TABLE {_QC_SCHEMA}.{_QC_TASKS} "
        f"ADD CONSTRAINT {_LEASE_GENERATION_CHECK} "
        "CHECK (lease_generation >= 0) NOT VALID"
    )
    op.execute(
        f"ALTER TABLE {_QC_SCHEMA}.{_QC_TASKS} "
        f"VALIDATE CONSTRAINT {_LEASE_GENERATION_CHECK}"
    )


_PROJECTION_FK = "fk_task_projection_project_tenant"


def _create_projection() -> None:
    op.execute(f"CREATE SCHEMA IF NOT EXISTS {_PROJECTION_SCHEMA}")
    op.create_table(
        _PROJECTION,
        # 主键就是源任务的 entity_id：投影不引入第二套 id 空间。
        sa.Column("task_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("task_status", sa.String(32), nullable=False),
        sa.Column("next_action_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_generation", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("entity_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        schema=_PROJECTION_SCHEMA,
    )
    # 归属外键：派生数据同样必须指向真实且同租户的项目。先 NOT VALID 再加锁较弱地
    # VALIDATE，与 ``test_ownership_fk_constraints`` 的口径一致（只建不验会被判红）。
    op.execute(
        f"ALTER TABLE {_PROJECTION_SCHEMA}.{_PROJECTION} "
        f"ADD CONSTRAINT {_PROJECTION_FK} "
        "FOREIGN KEY (project_id, tenant_id) "
        "REFERENCES project_planning.projects (entity_id, tenant_id) NOT VALID"
    )
    op.execute(
        f"ALTER TABLE {_PROJECTION_SCHEMA}.{_PROJECTION} VALIDATE CONSTRAINT {_PROJECTION_FK}"
    )
    # 两个部分索引：各自只覆盖「会用到它」的那一类行。全量复合索引会让每次
    # 状态写入都为两种查询各维护一份 B-tree，而调度只会命中其中一种。
    op.execute(
        f"CREATE INDEX ix_qc_projection_queued ON {_PROJECTION_SCHEMA}.{_PROJECTION} "
        "(next_action_at, task_id) WHERE task_status = 'QUEUED'"
    )
    op.execute(
        f"CREATE INDEX ix_qc_projection_running ON {_PROJECTION_SCHEMA}.{_PROJECTION} "
        "(lease_expires_at, task_id) WHERE task_status = 'RUNNING'"
    )


def _projection_visibility() -> str:
    """与 qc_tasks 同形的可见性：租户一致 + 项目在其租户内真实存在。

    投影不复制业务内容，但它能暴露「哪个项目有哪些任务、租约何时到期」，
    因此不能放成无 RLS 的公共表。
    """
    return (
        "USING ("
        f"{tenant_scope_predicate()} AND "
        "EXISTS (SELECT 1 FROM project_planning.projects p "
        f"WHERE p.entity_id = {_PROJECTION_SCHEMA}.{_PROJECTION}.project_id "
        f"AND p.tenant_id = {_PROJECTION_SCHEMA}.{_PROJECTION}.tenant_id)"
        ")"
    )


def _enable_projection_rls() -> None:
    op.execute(
        f"ALTER TABLE {_PROJECTION_SCHEMA}.{_PROJECTION} ENABLE ROW LEVEL SECURITY"
    )
    op.execute(
        f"CREATE POLICY tenant_isolation_{_PROJECTION} "
        f"ON {_PROJECTION_SCHEMA}.{_PROJECTION} {_projection_visibility()}"
    )
    # FORCE：表所有者自己也走策略。否则「以所有者身份跑的维护脚本」会成为
    # 一个不需要租户上下文的旁路。
    op.execute(
        f"ALTER TABLE {_PROJECTION_SCHEMA}.{_PROJECTION} FORCE ROW LEVEL SECURITY"
    )


def upgrade() -> None:
    _add_lease_generation()
    _create_projection()
    _enable_projection_rls()
    # 授权只经矩阵收敛（``data.role_grants_least_privilege`` 逐条比对）。
    # ``tables`` 必须显式传本迁移的快照：迁移是时间胶囊，读活矩阵会在未来
    # 新表的建表迁移之前先对它 REVOKE ALL，clean install 直接失败。
    for statement in converge_statements(
        domain_role(QC_GOVERNANCE), QC_GOVERNANCE, tables=frozenset({_PROJECTION})
    ):
        op.execute(statement)


def downgrade() -> None:
    op.execute(
        f"DROP POLICY IF EXISTS tenant_isolation_{_PROJECTION} "
        f"ON {_PROJECTION_SCHEMA}.{_PROJECTION}"
    )
    op.execute(
        f"ALTER TABLE {_PROJECTION_SCHEMA}.{_PROJECTION} DISABLE ROW LEVEL SECURITY"
    )
    op.execute(f"DROP INDEX {_PROJECTION_SCHEMA}.ix_qc_projection_running")
    op.execute(f"DROP INDEX {_PROJECTION_SCHEMA}.ix_qc_projection_queued")
    op.drop_table(_PROJECTION, schema=_PROJECTION_SCHEMA)
    op.execute(f"DROP SCHEMA IF EXISTS {_PROJECTION_SCHEMA}")
    op.execute(
        f"ALTER TABLE {_QC_SCHEMA}.{_QC_TASKS} DROP CONSTRAINT {_LEASE_GENERATION_CHECK}"
    )
    op.execute(
        f"ALTER TABLE {_QC_SCHEMA}.{_QC_TASKS} DROP COLUMN lease_generation"
    )
