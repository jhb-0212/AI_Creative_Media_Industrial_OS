"""add status check constraints

把已落库四张表的 status 列收口到契约状态集（C.3.1：未知枚举值必须拒绝）。

为什么这一步是必须的，而不是「Service 已经校验了」
------------------------------------------------
契约本轮把 39 个 lifecycle 对象的 `status` 收成枚举，运行时只实现了四个对象。
如果数据库仍接受任意字符串，那么「契约收紧了」只对走 Service 的代码生效：
任何直接 SQL、迁移脚本、后台修复、别的服务写入，都能往 status 里塞 `IN_PROGRESS`
或空字符串——而契约、Domain Enum、集成测试全部看不见。约束必须放在最可靠的那一层。

值列表为什么写死在这里，而不是 import 领域枚举
--------------------------------------------
迁移是**历史快照**：它描述「这一刻数据库应该长什么样」。若 import 的是活代码，
将来枚举增删会让这条历史迁移的行为随之改变，重建库的结果与当初上线的库不一致，
回滚语义也跟着漂。所以这里抄一份当时的契约值；「活的一致性」由两处保证——
模型层的 `status_check()`（同一出处生成）与
`scripts/contract_check.py::runtime_status_problems`（逐字比对模型约束与契约）。

Revision ID: c7d8e9f0a1b2
Revises: b3c4d5e6f7a8
Create Date: 2026-11-18 17:20:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

# revision identifiers, used by Alembic.
revision: str = "c7d8e9f0a1b2"
down_revision: str | Sequence[str] | None = "b3c4d5e6f7a8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (schema, table, 契约状态集快照)
# 状态集来源：contracts/state_machines/state_machines.yaml 的 allowed_states
# （本迁移落地时的快照，按字母序，与 status_check_sql 的输出逐字一致）。
STATUS_TABLES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "project_planning",
        "projects",
        ("ACTIVE", "ARCHIVED", "COMPLETED", "DRAFT", "ON_HOLD"),
    ),
    ("project_planning", "episodes", ("ACTIVE", "ARCHIVED", "DRAFT")),
    (
        "project_planning",
        "script_revisions",
        ("DRAFT", "LOCKED", "LOCK_PENDING", "SUPERSEDED"),
    ),
    (
        "asset_service",
        "asset_versions",
        (
            "APPROVED",
            "ARCHIVED",
            "GENERATED",
            "GOLD",
            "HUMAN_REVIEW",
            "QUARANTINED",
            "REJECTED",
            "REWORK",
            "SUPERSEDED",
            "TECHNICAL_QC",
        ),
    ),
)


def _constraint_name(table: str) -> str:
    return f"ck_{table}_status"


def _expr(values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{value}'" for value in values)
    return f"status IN ({quoted})"


def _assert_existing_rows_valid() -> None:
    """加约束前先自检：存量数据若已违反契约，给出可诊断的报错。

    直接 ADD CONSTRAINT 也能拦住，但 PostgreSQL 只会说「check constraint is
    violated by some row」——不告诉你哪张表、哪些值。数据修复要的是后者，
    所以这里先把违规值查出来再说。
    """
    if context.is_offline_mode():
        # 离线模式（alembic --sql）拿不到连接，也查不到存量数据；
        # 此时只渲染 DDL，自检留到真正执行时。
        return
    bind = op.get_bind()
    for schema, table, values in STATUS_TABLES:
        allowed = ", ".join(f"'{value}'" for value in values)
        rows = bind.execute(
            sa.text(
                f"SELECT status, count(*) FROM {schema}.{table} "
                f"WHERE status IS NULL OR status NOT IN ({allowed}) "
                "GROUP BY status ORDER BY count(*) DESC"
            )
        ).fetchall()
        if rows:
            detail = ", ".join(f"{status!r} × {count}" for status, count in rows)
            raise RuntimeError(
                f"{schema}.{table} 存在契约不允许的 status 值: {detail}；"
                "请先按契约修正存量数据，或把该值补进 state_machines.allowed_states "
                "（后者属于契约修订，不能只为让迁移通过）"
            )


def upgrade() -> None:
    """Upgrade schema: 为四张已落库表添加 status CHECK 约束。"""
    _assert_existing_rows_valid()
    for schema, table, values in STATUS_TABLES:
        op.create_check_constraint(
            _constraint_name(table),
            table,
            _expr(values),
            schema=schema,
        )


def downgrade() -> None:
    """Downgrade schema: 移除 status CHECK 约束。"""
    for schema, table, _ in STATUS_TABLES:
        op.drop_constraint(
            _constraint_name(table),
            table,
            schema=schema,
            type_="check",
        )
