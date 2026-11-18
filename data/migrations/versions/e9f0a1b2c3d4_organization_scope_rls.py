"""组织维度 RLS + FORCE（ADR-R24 D3a，核验报告 N-03）

问题（根因，不是「漏写策略」）
------------------------------
11 条既有策略一字不差，都只有租户维度：

    USING (tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid)

实测后果：``project_planning_role`` 在单租户上下文里可读 **51 个组织、1584 个项目**，
组织边界 100% 由应用层 ``authorize`` 判定。任何绕过 authorize 的访问（新端点忘了挂
依赖、Worker 直查、脚本直连）都会让组织边界静默失效，而数据库不会拒绝。

根因是**策略缺输入**：``Principal`` 里早有组织信息，但从未注入数据库会话。
缺一个维度，就只能写出一个维度的策略——写十条也一样。

本迁移做三件事
--------------
1. 组织上下文与租户上下文同点注入（``packages/shared/rls.py``），策略改为
   「租户 + 组织」复合谓词；
2. 谓词由 ``organization_scope_predicate()`` 生成，**不在迁移里手抄 SQL**——
   与应用层 ``assert_ownership_scope`` 是同一规则的两种执行位置（ADR-R24 §D3a-2），
   手抄会在规则变化时产生第二份副本；
3. 业务表与证据表 ``FORCE ROW LEVEL SECURITY``。

子资源（episodes / script_revisions / asset_versions / qc_reports）没有
``organization_id`` 列（归属经 project 派生，冗余存储会产生第二事实源），
它们的组织判定用**父表存在性**表达，形如：

    EXISTS (SELECT 1 FROM project_planning.projects p
            WHERE p.entity_id = <本表>.project_id AND p.tenant_id = <本表>.tenant_id)

``projects`` 自己的策略会在子查询内生效，于是组织规则只写一次、子表传递获得。
代价是每行一次主键探测，收益是组织判定不可能在子表处成为另一份副本。

不在本次范围
------------
- ``outbox.outbox_events``：Relay 需要跨租户枚举（迁移 e5f6a7b8c9d0 的窄通道），
  给它加组织维度会把「投递基础设施」与「业务授权」耦合。事件总线作用域是租户。
- ``audit.status_transitions``：子资源的组织归属尚未落值（ADR-R23 §7.4），
  此时加 ``organization_id IS NULL OR ...`` 的谓词，等于把未落值的子资源事实
  判给所有组织可见——比不加更糟。按 ADR-R24 §6 债务 2 处理。

Revision ID: e9f0a1b2c3d4
Revises: d8e9f0a1b2c3
Create Date: 2026-11-18
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

from packages.shared.rls import (
    organization_scope_predicate,
    tenant_scope_predicate,
)

revision: str = "e9f0a1b2c3d4"
down_revision: str | Sequence[str] | None = "d8e9f0a1b2c3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PP = "project_planning"
ASSET = "asset_service"
AUDIT = "audit"


def _policy_name(table: str) -> str:
    """策略名与既有一字不差：排障时一眼认出是哪张表的策略。"""
    return f"tenant_isolation_{table}"


def _tenant_only(column: str = "tenant_id") -> str:
    """单维策略（`downgrade` 用）：恢复迁移前的形态。"""
    return f"USING ({tenant_scope_predicate(column)})"


def _combined(*clauses: str) -> str:
    """把多个谓词用 AND 连成一个 `USING`/`WITH CHECK` 子句。"""
    return "USING (" + " AND ".join(f"({clause})" for clause in clauses) + ")"


def _project_exists(schema: str, table: str) -> str:
    """子表 → projects 的存在性谓词（组织规则由 projects 的策略在子查询内执行）。"""
    return (
        "EXISTS (SELECT 1 FROM project_planning.projects p "
        f"WHERE p.entity_id = {schema}.{table}.project_id "
        f"AND p.tenant_id = {schema}.{table}.tenant_id)"
    )


# 需要加组织维度的表：表 → 组织谓词。
# 「organizations 表自身的组织列是它的主键」是唯一一处规则形态不同，理由在语义：
# 组织行就是归属链的根，不继承自父级。
_SCOPED_TABLES: tuple[tuple[str, str, str], ...] = (
    # (schema, table, organization predicate)
    (PP, "projects", organization_scope_predicate()),
    (PP, "organizations", organization_scope_predicate("organization_id")),
    (PP, "episodes", _project_exists(PP, "episodes")),
    (PP, "script_revisions", _project_exists(PP, "script_revisions")),
    (ASSET, "asset_versions", _project_exists(ASSET, "asset_versions")),
    (
        ASSET,
        "qc_reports",
        # QC 报告没有 project_id：链是 qc_reports → asset_versions → projects，
        # 与归属解析（packages/identity/ownership.py::_RESOLVE_SQL["QCReport"]）
        # 同一条路径，避免「授权按一条链、RLS 按另一条链」。
        "EXISTS (SELECT 1 FROM asset_service.asset_versions a "
        "JOIN project_planning.projects p "
        "ON p.entity_id = a.project_id AND p.tenant_id = a.tenant_id "
        "WHERE a.entity_id = asset_service.qc_reports.asset_version_id "
        "AND a.tenant_id = asset_service.qc_reports.tenant_id)",
    ),
)

# FORCE 也要覆盖证据表：它已 ENABLE，但所有者（当前是超级用户）不受策略约束。
# 注意 FORCE 对超级用户/BYPASSRLS 无效（PG 语义），它的作用是「将来所有权转给
# 非 BYPASSRLS 角色后 RLS 不会静默失效」；真正的防线是启动期检查（ADR-R24 §D3a-5）。
_FORCE_TABLES: tuple[tuple[str, str], ...] = (
    (PP, "projects"),
    (PP, "organizations"),
    (PP, "episodes"),
    (PP, "script_revisions"),
    (ASSET, "asset_versions"),
    (ASSET, "qc_reports"),
    (AUDIT, "status_transitions"),
)


def upgrade() -> None:
    """把 6 张表的策略升级为「租户 + 组织」，并对业务与证据表启用 FORCE。"""
    for schema, table, org_clause in _SCOPED_TABLES:
        policy = _policy_name(table)
        op.execute(f"DROP POLICY IF EXISTS {policy} ON {schema}.{table}")
        op.execute(
            f"CREATE POLICY {policy} ON {schema}.{table} "
            + _combined(tenant_scope_predicate(), org_clause)
        )

    for schema, table in _FORCE_TABLES:
        op.execute(f"ALTER TABLE {schema}.{table} FORCE ROW LEVEL SECURITY")


def downgrade() -> None:
    """恢复单维（租户）策略并取消 FORCE，回到迁移前形态。"""
    for schema, table in reversed(_FORCE_TABLES):
        op.execute(f"ALTER TABLE {schema}.{table} NO FORCE ROW LEVEL SECURITY")

    for schema, table, _ in reversed(_SCOPED_TABLES):
        policy = _policy_name(table)
        op.execute(f"DROP POLICY IF EXISTS {policy} ON {schema}.{table}")
        op.execute(
            f"CREATE POLICY {policy} ON {schema}.{table} " + _tenant_only()
        )


__all__: Sequence[str] = ("downgrade", "upgrade")
