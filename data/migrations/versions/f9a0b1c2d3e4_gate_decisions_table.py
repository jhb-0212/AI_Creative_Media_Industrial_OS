"""Gate 决策记录落库：建表 + RLS + 授权（过渡期 project_planning schema）

为什么必须有这张表（根因）
- 决策记录是「这个 Gate 已经裁决」的唯一证据。R16 首版路由把它放在进程内存里，
  于是：进程重启 → 决策消失；而 outbox 事件仍留在库里指向一个不存在的聚合。
  审计两侧从此对不上，且没有任何闸门会红——因为「漏持久化」不产生错误，只产生
  一段查不到的空白。
- 决策是追加式不可变记录（envelope_only / NO_LIFECYCLE）：只 INSERT/SELECT，
  不 UPDATE/DELETE。这条不变量同时由授权矩阵（无 UPDATE/DELETE 权限）与
  status_check（唯一允许 NO_LIFECYCLE）表达，而不是靠代码自觉。

为什么与其余四表同落 project_planning（过渡口径）
- 见 e8f9a0b1c2d3 的同段说明：独立域拆分（qc_governance 自有 schema + role）需要
  lifespan 多池 + readiness 同步，属 Phase 2/3；此处沿用同角色同 RLS。

库层不变量：
1. 信封列与 C.3.1 逐列一致；tenant_id 参与 RLS。
2. 组织可见性经父项目存在性表达（gate_decisions 自带 project_id，不冗余
   organization_id，项目换组织时不会留下第二事实源）。
3. decision 受控（status_check 由 NoLifecycleStatus 生成）——决策是数据，不是
   状态的第二种表示，因此本表的 CHECK 只约束 NO_LIFECYCLE 哨兵。

Revision ID: f9a0b1c2d3e4
Revises: e8f9a0b1c2d3
Create Date: 2026-11-22
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

from packages.shared.db_grants import PROJECT_PLANNING, converge_statements
from packages.shared.db_roles import domain_role
from packages.shared.rls import tenant_scope_predicate
from packages.shared.status_constraint import status_check
from services.project_planning.domain.enums import NoLifecycleStatus

revision: str = "f9a0b1c2d3e4"
down_revision: str | Sequence[str] | None = "e8f9a0b1c2d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCHEMA = PROJECT_PLANNING
_TABLE = "gate_decisions"
_TABLES = (_TABLE,)


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


def _project_visibility() -> str:
    """项目作用域可见性：租户 + 父项目存在（子表不冗余 organization_id）。"""
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
        sa.Column("gate_key", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("decided_by", sa.String(64), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("gate_instance_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("constraint_snapshot_id", sa.String(128), nullable=False, server_default=""),
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
        status_check((s.value for s in NoLifecycleStatus), table=_TABLE),
        schema=_SCHEMA,
    )


def _create_indexes() -> None:
    op.create_index(
        op.f(f"ix_project_planning_{_TABLE}_tenant_id"),
        _TABLE,
        ["tenant_id"],
        schema=_SCHEMA,
    )
    op.create_index(
        op.f(f"ix_project_planning_{_TABLE}_project_id"),
        _TABLE,
        ["project_id"],
        schema=_SCHEMA,
    )
    op.create_index(
        op.f(f"ix_project_planning_{_TABLE}_gate_key"),
        _TABLE,
        ["project_id", "gate_key"],
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
    """回滚：撤策略、关 RLS、删表。

    为什么不逐条 ``DROP INDEX``（D-02）
    这里的三个索引由本迁移 upgrade 建在 ``gate_decisions`` 上，索引随表存在，
    ``DROP TABLE`` 会连索引一起删——逐条删是**冗余**，而冗余在这条链上会变成
    错误：后面的 ``a0b1c2d3e4f5``（R16 域拆分 cutover）把这张表搬走时是
    「先删旧表、再在新 schema 重建」，降级时又反过来重建回 ``project_planning``。
    所以本迁移降级时面对的索引名**不是**自己 upgrade 时那一组：

    | 场景 | 表上的索引名 |
    | --- | --- |
    | 升到 ``f9a0b1c2d3e4`` 就降级（未过 a0b1） | ``ix_project_planning_gate_decisions_<列>``（本迁移建） |
    | 从 head 降级（已过 a0b1） | 由 ``a0b1`` 的降级重建，多列索引带全部列名 |
    | 当前 head 上实际存在 | ``ix_qc_governance_gate_decisions_<列>`` |

    按名字删只能命中其中一种，另外两种必然 ``does not exist``。真实集群实测：
    ``head → 7a8b9c0d1e2f`` 死在第 40 位，报
    ``index "ix_project_planning_gate_decisions_gate_key" does not exist``。

    也不允许改成 ``DROP INDEX IF EXISTS``：那会把"名字算错"变成"静默跳过"，
    下次真少了一个索引也没人知道。正确做法是把索引交给拥有它的表——删表即删索引，
    名字对不对都不用迁移来猜。
    """
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation_{_TABLE} ON {_SCHEMA}.{_TABLE}")
    op.execute(f"ALTER TABLE {_SCHEMA}.{_TABLE} DISABLE ROW LEVEL SECURITY")
    op.drop_table(_TABLE, schema=_SCHEMA)
