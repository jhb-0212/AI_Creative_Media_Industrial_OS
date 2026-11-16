"""补齐归属链：organization_id（全实体）+ project_id（资产与 QC 证据）

根因（审查第三轮 F2「数据所有权」的后半段）：
租户 RLS 只回答"哪家客户"，不回答"客户内部哪个主体、哪个项目"。
具体表现有三处，缺任何一处都无法做项目级授权：

1. ``projects`` 等实体只有 ``tenant_id``，没有 ``organization_id``；
   而 Action Registry 里大量动作的 scope 写作 ``tenant+organization``——
   契约声明了范围，数据里却没有承载它的列。
2. ``asset_versions`` **没有任何项目归属字段**。资产是悬空的：无法回答
   "这个资产属于哪个项目"，于是同租户内不同项目之间不存在隔离。
3. ``qc_reports`` 同样没有项目归属，检测证据无法被项目级授权覆盖。

本迁移只加列与回填，不改业务逻辑：
- 四个继承 C.3.1 信封的表（projects / episodes / script_revisions /
  asset_versions）加 ``organization_id``，回填为 ``tenant_id``
  （单组织租户基线：一个租户当前只有一个制作主体）；
- ``asset_versions`` / ``qc_reports`` 加 ``project_id``，历史数据回填为
  ``legacy-unassigned``。

关于 ``legacy-unassigned``：这是**显式的遗留标记**，不是伪造归属。
迁移不能凭空知道 165 条历史资产属于哪个项目；与其留 NULL（让"未归属"
在查询里隐形），不如给一个可检索的哨兵值，让这批数据在报表里可见、
可被后续人工或脚本重新归属。新写入由 API 层强制 ``project_id`` 必填，
且有 CHECK 约束不允许新行落到该哨兵值。

Revision ID: d3e4f5a6b7c8
Revises: c2d3e4f5a6b7
Create Date: 2026-11-16
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d3e4f5a6b7c8"
down_revision: str | Sequence[str] | None = "c2d3e4f5a6b7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PROJECT_SCHEMA = "project_planning"
ASSET_SCHEMA = "asset_service"

#: 历史资产的占位项目标识（见模块 docstring 第 4 段）。
LEGACY_UNASSIGNED_PROJECT = "legacy-unassigned"

#: 继承 C.3.1 信封（EntityMixin）的表都需要 organization_id。
_ENVELOPE_TABLES: tuple[tuple[str, str], ...] = (
    (PROJECT_SCHEMA, "projects"),
    (PROJECT_SCHEMA, "episodes"),
    (PROJECT_SCHEMA, "script_revisions"),
    (ASSET_SCHEMA, "asset_versions"),
)

#: 需要组织归属的全部表 = 信封表 + 不带信封的证据表（qc_reports）。
#: qc_reports 不继承信封（它是证据附件，没有 entity_id/状态机），但它同样
#: 受"组织 → 项目 → 资源"归属链约束。让它也带 organization_id，是为了让
#: 五张项目级资源表上的隔离策略**文本一致**：同一份规则五种写法，
#: 迟早会有一张表被漏掉，而漏掉的那张不会报错，只会静默泄漏。
_ORG_TABLES: tuple[tuple[str, str], ...] = (*_ENVELOPE_TABLES, (ASSET_SCHEMA, "qc_reports"))

#: 带"内容不可变"触发器的表（ADR-R11）。回填历史数据必须临时停用用户触发器，
#: 否则连组织归属这种结构性列的补齐都会被拦（首次执行即被拦，是触发器有效的
#: 证据）。停用范围仅限本事务内的这两张表，事务结束（含失败回滚）自动恢复。
_IMMUTABLE_TABLES: frozenset[tuple[str, str]] = frozenset(
    {(ASSET_SCHEMA, "asset_versions"), (ASSET_SCHEMA, "qc_reports")}
)


def _backfill(schema: str, table: str, statement: str) -> None:
    """执行回填语句；不可变表临时停用用户触发器。"""
    if (schema, table) not in _IMMUTABLE_TABLES:
        op.execute(statement)
        return
    op.execute(f"ALTER TABLE {schema}.{table} DISABLE TRIGGER USER")
    op.execute(statement)
    op.execute(f"ALTER TABLE {schema}.{table} ENABLE TRIGGER USER")


def _add_organization_id() -> None:
    """加 organization_id 并回填为 tenant_id（单组织租户基线）。"""
    for schema, table in _ORG_TABLES:
        op.add_column(
            table, sa.Column("organization_id", sa.Uuid(), nullable=True), schema=schema
        )
        _backfill(
            schema,
            table,
            f"UPDATE {schema}.{table} SET organization_id = tenant_id "
            f"WHERE organization_id IS NULL",
        )
        op.alter_column(table, "organization_id", nullable=False, schema=schema)
        op.create_index(
            op.f(f"ix_{schema}_{table}_organization_id"),
            table,
            ["organization_id"],
            unique=False,
            schema=schema,
        )


def _add_project_id(schema: str, table: str) -> None:
    """加 project_id、回填历史哨兵值、用触发器禁止新行落到哨兵值。

    为什么是触发器而不是 CHECK 约束：CHECK 会连"回填历史数据"一起拒掉，
    而时间边界又不可靠（历史数据的 created_at 可能就在迁移当天）。触发器
    只约束 **INSERT**，精确表达"新写入必须有真实归属"这一条意图。

    这也与 ADR-R11 的既有做法一致：不变量放在最可靠的层级（数据库）。
    """
    op.add_column(
        table, sa.Column("project_id", sa.String(64), nullable=True), schema=schema
    )
    _backfill(
        schema,
        table,
        f"UPDATE {schema}.{table} SET project_id = '{LEGACY_UNASSIGNED_PROJECT}' "
        f"WHERE project_id IS NULL",
    )
    op.alter_column(table, "project_id", nullable=False, schema=schema)
    op.create_index(
        op.f(f"ix_{schema}_{table}_project_id"),
        table,
        ["project_id"],
        unique=False,
        schema=schema,
    )
    _add_assignment_guard(schema, table)


def _add_assignment_guard(schema: str, table: str) -> None:
    """新增行不得使用遗留哨兵值（历史行不受影响）。"""
    function = f"{schema}.assert_{table}_project_assigned"
    op.execute(
        f"""
        CREATE FUNCTION {function}() RETURNS trigger AS $$
        BEGIN
            IF NEW.project_id = '{LEGACY_UNASSIGNED_PROJECT}' THEN
                RAISE EXCEPTION
                    '新写入必须带真实 project_id（{LEGACY_UNASSIGNED_PROJECT} '
                    '仅用于迁移前的历史数据）';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER {table}_project_assigned
        BEFORE INSERT ON {schema}.{table}
        FOR EACH ROW EXECUTE FUNCTION {function}()
        """
    )


def _drop_assignment_guard(schema: str, table: str) -> None:
    op.execute(f"DROP TRIGGER IF EXISTS {table}_project_assigned ON {schema}.{table}")
    op.execute(
        f"DROP FUNCTION IF EXISTS {schema}.assert_{table}_project_assigned()"
    )


def upgrade() -> None:
    """补齐组织归属与项目归属。"""
    _add_organization_id()
    _add_project_id(ASSET_SCHEMA, "asset_versions")
    _add_project_id(ASSET_SCHEMA, "qc_reports")


def downgrade() -> None:
    """回滚：删列与约束（数据随之丢弃，仅本地回退使用）。"""
    for schema, table in (
        (ASSET_SCHEMA, "qc_reports"),
        (ASSET_SCHEMA, "asset_versions"),
    ):
        _drop_assignment_guard(schema, table)
        op.drop_index(
            op.f(f"ix_{schema}_{table}_project_id"), table_name=table, schema=schema
        )
        op.drop_column(table, "project_id", schema=schema)

    for schema, table in reversed(_ORG_TABLES):
        op.drop_index(
            op.f(f"ix_{schema}_{table}_organization_id"),
            table_name=table,
            schema=schema,
        )
        op.drop_column(table, "organization_id", schema=schema)
