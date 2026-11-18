"""业务域角色的表级最小权限（DATA-6 / N-06 根治）

问题（运行库实测，不是读代码推断）
------------------------------------------------------------
``project_planning_role`` 持有：:

    project_planning.projects        SELECT/INSERT/UPDATE/DELETE
    project_planning.organizations   SELECT/INSERT/UPDATE/DELETE
    project_planning.episodes        SELECT/INSERT/UPDATE/DELETE
    project_planning.script_revisions SELECT/INSERT/UPDATE/DELETE

而 ``contracts/action_registry`` 里**没有任何 ORGANIZATION.* 动作，也没有任何
*.DELETE 动作**（删除语义是 ARCHIVED / SUPERSEDED，不是物理删除）。也就是说
数据库授予的权限面比契约声明的动作面更宽，宽出来的部分无人审计、无人授权、
无人负责。

根因（两层，缺一层都修不掉）
------------------------------------------------------------
1. **授权从未被声明**。``8f3b2c1d4e5a`` 用
   ``GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA`` 表达
   「这个域能读写自己的 schema」——权限面由**表清单**决定，不由**动作清单**决定。
   于是新增表自动多一份全量 DML，而动作清单里根本没有 DELETE。
2. **默认权限把同一个根因留给了「下一次建表」**。``8f3b2c1d4e5a`` 同时给两个
   域角色授了 ``ALTER DEFAULT PRIVILEGES ... GRANT SELECT, INSERT, UPDATE,
   DELETE ON TABLES``。``b7c8d9e0f1a2`` 的「默认零授权」只 REVOKE 了
   ``app_role``——域角色是后建的，于是「新表自动获得全量 DML」这条旧机制
   在新角色上原样复发（实测 ``pg_default_acl`` 里两个域角色对业务 schema
   都持 DELETE）。只 REVOKE 现有表，等于把缺陷留给未来。

本迁移做什么
------------------------------------------------------------
逐条执行 ``packages/shared/db_grants.py::converge_statements`` 生成的语句：
每张表 ``REVOKE ALL``（含对 ``PUBLIC``）后按矩阵 ``GRANT``，并撤销业务 schema
的默认权限。**本文件不含任何手写 GRANT**——矩阵是唯一声明处，迁移只是它的执行器。

**本迁移是时间胶囊，不是矩阵的镜像**（评审 F1）。它显式传
``_GRANTED_AT_THIS_REVISION``：只处理「写下这个 revision 时矩阵里已存在的表」。
若不传，未来矩阵新增一张表时，本迁移会在**该表的建表迁移之前**对它
``REVOKE ALL`` —— clean install 直接失败（表还不存在），而已升级的库又不会重跑
本迁移（新表于是永远没有授权）。新表的授权属于**新增迁移**的职责：在建表之后
调用 ``converge_statements(..., tables={"新表"})``。判据由断言
``data.role_grants_least_privilege`` 保证「矩阵里每张表都被某条迁移覆盖」。

另加一条运行时护栏：收敛后枚举业务 schema 的真实表，若存在矩阵未声明的表
直接失败。理由是「矩阵覆盖」不能只靠 ORM 静态比对——迁移建的表（如未来的
证据表）不在 ORM 里，静态断言看不见它。

Revision ID: c1a2b3d4e5f6
Revises: e9f0a1b2c3d4
Create Date: 2026-11-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from packages.shared.db_grants import (
    DOMAINS,
    business_tables,
    converge_statements,
    legacy_statements,
)
from packages.shared.db_roles import domain_role

revision: str = "c1a2b3d4e5f6"
down_revision: str | Sequence[str] | None = "e9f0a1b2c3d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LIST_TABLES = sa.text(
    """
    SELECT table_name
    FROM information_schema.tables
    WHERE table_schema = :schema AND table_type = 'BASE TABLE'
    ORDER BY table_name
    """
)

# 本 revision 时刻矩阵里的表（冻结，不随 db_grants 的未来增补而变化）。
# 只写表名不写权限：权限仍由矩阵生成——收紧/放宽一个权限不需要新迁移，
# 而「多了一张表」必须由新迁移在建表之后显式声明。
_GRANTED_AT_THIS_REVISION: frozenset[str] = frozenset(
    {
        "projects",
        "episodes",
        "script_revisions",
        "organizations",
        "asset_versions",
        "qc_reports",
        "status_transitions",
        "outbox_events",
        "revoked_tokens",
        "alembic_version",
    }
)


def _actual_tables(schema: str) -> set[str]:
    """业务 schema 里真实存在的表名（排除分区子表：它们已在父表口径内）。"""
    bind = op.get_bind()
    rows = bind.execute(_LIST_TABLES, {"schema": schema}).scalars()
    return set(rows)


def _undeclared(schema: str) -> list[str]:
    """存在于数据库、但矩阵未声明的表（漏声明即失败，不静默无权限运行）。"""
    declared = {table for s, table in business_tables(schema) if s == schema}
    return sorted(_actual_tables(schema) - declared)


def upgrade() -> None:
    """按矩阵收敛两个业务域角色的表级权限，并撤销业务 schema 的默认权限。

    ``_GRANTED_AT_THIS_REVISION`` 是本迁移的**冻结范围**：clean install 时后建的
    表还不在数据库里，对未来表的 ``REVOKE ALL`` 会直接失败；已升级的库又不会
    重跑本迁移。新表由新迁移在建表之后声明（调用同一生成器，不手写 GRANT）。
    """
    for domain in DOMAINS:
        role = domain_role(domain)
        for statement in converge_statements(
            role, domain, tables=_GRANTED_AT_THIS_REVISION
        ):
            op.execute(statement)

    # 护栏：收敛后若还有未声明的表，说明矩阵没跟上 schema 演进。
    # fail-closed 放在这里而不是留给运行时：新表在没有声明的情况下会「能建不能写」，
    # 排障时会先怀疑权限配置，而真正的原因是没人决定过它该有什么权限。
    undeclared = [table for domain in DOMAINS for table in _undeclared(domain)]
    if undeclared:
        raise RuntimeError(
            "业务 schema 存在矩阵未声明的表：" + ", ".join(undeclared) + "。"
            "请在 packages/shared/db_grants.py 声明其最小权限与真实写入路径，"
            "再重跑本迁移——未声明的表不允许带任何权限上线。"
        )


def downgrade() -> None:
    """回到「业务 schema 全量 DML + 全量默认权限」的旧状态（仅用于本地回退）。"""
    for domain in DOMAINS:
        role = domain_role(domain)
        for statement in legacy_statements(role, domain):
            op.execute(statement)
