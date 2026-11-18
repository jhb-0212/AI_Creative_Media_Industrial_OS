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
每张表 ``REVOKE ALL`` 后按矩阵 ``GRANT``，并撤销业务 schema 的默认权限。
**本文件不含任何手写 GRANT**——矩阵是唯一声明处，迁移只是它的执行器；
断言 ``data.role_grants_least_privilege`` 校验两者逐条相等，任何一边漂移即红。

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
    """按矩阵收敛两个业务域角色的表级权限，并撤销业务 schema 的默认权限。"""
    for domain in DOMAINS:
        role = domain_role(domain)
        for statement in converge_statements(role, domain):
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
