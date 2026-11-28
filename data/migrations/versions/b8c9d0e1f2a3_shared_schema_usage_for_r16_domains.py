"""补齐各域角色对共享 schema 的 USAGE（授权面缺口）

缺陷是什么
----------
``production_planning_role`` / ``budget_quota_role`` / ``qc_governance_role``
在 ``outbox`` / ``audit`` / ``auth`` 三个 schema 上**都有表权限**，却没有
``USAGE ON SCHEMA``。PostgreSQL 里这两者是独立的两道门：

    GRANT INSERT ON outbox.outbox_events TO r;   -- 有
    GRANT USAGE  ON SCHEMA outbox        TO r;   -- 没有 → 第一次 INSERT 即 42501

于是这三个域的所有写路径在真实环境下都是 500：

- ``GENERATION.*``（claim / renew / finish）→ 写 GENERATION_STARTED 即失败；
- ``BUDGET.SHOT.APPROVE`` → 账本与审批事件同事务，事件写不进去即回滚；
- ``GATE.*`` / ``QC.TASK.*`` → 状态迁移事实与事件同理。

为什么此前没有任何测试发现
--------------------------
1. 单测用替身会话，不碰 ACL；
2. 集成测试直接拿域角色**执行 SQL**，写入的都是域自有的表，从不写 outbox；
3. 授权守卫 ``test_domain_role_privileges_match_matrix`` 比对的是
   ``pg_class.relacl``（表级），而 ``nspacl``（schema 级）在结构上不在它视野内。

「表权限在、ACL 对账绿、功能整体不可用」是本条最危险的地方：所有静态信号都
正常，只有真实调用会炸。本迁移补上这两道门里缺的那一道，并另加一条从矩阵
派生的 schema 级守卫（``test_domain_role_has_usage_on_every_referenced_schema``），
让同类缺口下次立刻报红。

根因与根治
----------
根因不在迁移写漏了一句，而在**授权面有两个 ACL 位置，而矩阵只声明了其中一个**。
``8f3b2c1d4e5a`` 用一句手写的 ``GRANT USAGE ON SCHEMA outbox`` 覆盖了**当时**
存在的两个域角色；矩阵（``packages/shared/db_grants.converge_statements``）接管
授权面之后，只硬编码补了「本域 + project_planning」的 USAGE，于是矩阵之后新建
的域天生缺少共享 schema 的 USAGE——每新建一个域就复发一次。

所以根治分两半，本迁移只是其中「修存量」的那一半：

- ``db_grants.referenced_schemas`` / ``schema_usage_statements``：USAGE 集合改为
  从矩阵**派生**（「他在这个 schema 下有表权限 ⇒ 他需要这个 schema 的 USAGE」），
  新建域时由它自己的迁移自动带上，不再依赖谁记得手写一句；
- 本迁移：把三个已存在的域补到与派生结果一致。

为什么这里写死三个域而不是全量派生
----------------------------------
迁移是时间胶囊（评审 F1）：它记录「此刻哪三个域缺 USAGE」。将来新增的域由它
自己的迁移负责，本迁移不应在**建 schema 之前**去 GRANT 一个还不存在的 schema。
``schema_usage_statements`` 仍然是派生函数，写死的只是「降级时撤销谁」这个
历史事实，不是「需要哪些 schema」——后者一旦手抄就会再次分化。

升级侧对**全部域**收敛（幂等），降级侧只撤三个 R16 域
------------------------------------------------------
升级对全部域执行：``converge_statements`` 现在按矩阵派生 USAGE，而 DATA-6 的
「迁移语句集合 == 矩阵语句集合」要求渲染并集里出现这几句，project_planning /
asset_service 也在此列（它们早先靠手写语句拿到，矩阵接管后同样缺"声明"）。
对已持有 USAGE 的角色重复 ``GRANT`` 是空操作，因此全量执行既安全又让两侧对齐。

降级只撤三个 R16 域的共享 schema USAGE：对 project_planning / asset_service
而言，这些权限由 ``8f3b2c1d4e5a`` 授予，**不是本迁移的净变化**，撤销它会让
降级造成比升级前更坏的状态（那两个域当场不可用）。降级只回滚自己改过的东西。

Revision ID: b8c9d0e1f2a3
Revises: c4d5e6f7a8b9
Create Date: 2026-11-23
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

from packages.shared.db_grants import (
    BUDGET_QUOTA,
    DOMAINS,
    MAINTENANCE_SCHEMA,
    PRODUCTION_PLANNING,
    QC_GOVERNANCE,
    referenced_schemas,
    schema_usage_statements,
)
from packages.shared.db_roles import domain_role

revision: str = "b8c9d0e1f2a3"
down_revision: str | Sequence[str] | None = "c4d5e6f7a8b9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 本迁移**修复**的域（矩阵接管授权面之后才建立、天生缺共享 schema USAGE）。
_REPAIRED_DOMAINS = (PRODUCTION_PLANNING, BUDGET_QUOTA, QC_GOVERNANCE)


def _frozen_schemas(domain: str) -> tuple[str, ...]:
    """写下本 revision 时已存在的 schema（排除之后才建的维护 schema）。

    这是时间胶囊的另一半：表范围靠「当时的矩阵」冻结，schema 范围也必须冻结。
    维护 schema ``qc_maintenance`` 晚于本迁移才由 ``a8b9c0d1e2f3`` 创建；若照今
    天的派生结果对它 ``GRANT USAGE``，clean install 在读到这一句时就报
    ``schema "qc_maintenance" does not exist``——已升级的库不会重跑，缺口只在
    从零建库时暴露。
    """
    return tuple(schema for schema in referenced_schemas(domain) if schema != MAINTENANCE_SCHEMA)


def upgrade() -> None:
    """按矩阵派生结果补齐 USAGE（幂等：已授予时重复 GRANT 无副作用）。"""
    for domain in DOMAINS:
        role = domain_role(domain)
        for statement in schema_usage_statements(role, domain, schemas=_frozen_schemas(domain)):
            op.execute(statement)


def downgrade() -> None:
    """本迁移在父 revision 上**没有净新增授权**，降级不应撤销任何 USAGE。

    父态快照（P）显示：``audit`` / ``auth`` / ``outbox`` /
    ``project_planning`` / ``public`` 五个 schema 上本来就有六个域角色的
    USAGE（``8f3b2c1d4e5a`` 一次授全）。本迁移只是"幂等重复授予"，
    GRANT 的净效果为零；旧实现按"本迁移授了三条"去 REVOKE，等于把父态
    已有的授权一起撤掉——降级态反而比父态少了 3 个角色 × 5 个 schema
    （WP-04/#47）。回滚语义应是"不做任何权限变更"。
    """
