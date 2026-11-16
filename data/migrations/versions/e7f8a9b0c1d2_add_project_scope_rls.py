"""为项目级资源加 RLS「项目范围」限制性策略

根因（审查第三轮第 11 条：同租户不同项目未隔离）：
- 既有 RLS 只有一条 ``tenant_id = current_setting('app.current_tenant_id')``，
  它回答的是「哪家客户」，不是「客户内部的哪个项目」。
- 于是在同一个租户内，任意主体可以读写任意项目的集数、剧本、资产与 QC 证据。
  归属链（组织 → 项目 → 资源）在数据模型上补齐之后，拒绝逻辑必须落到**最可靠
  的层级**：数据库。应用层"每个 handler 记得查一次"是最容易漏的形态。

实现方式（为什么是限制性策略而不是改既有策略）：
- PostgreSQL 对同一命令的多条 **permissive** 策略取「或」，直接再加一条
  permissive 策略等于把租户隔离**放宽**成「租户匹配 或 项目匹配」——这是
  一个会把安全网撕开的口子。
- **RESTRICTIVE** 策略与既有 permissive 策略取「与」，语义正好是
  「租户隔离 且 项目在授权范围内」。因此本迁移不改既有策略，只做加法。

范围来源（两个 GUC，缺一不可）：
- ``app.current_org_id``：调用方所属组织（来自令牌，服务端写入）。
- ``app.current_projects``：持有本组织内全部项目范围时为字面量 ``*``，
  否则为逗号分隔的项目 id 列表。
- GUC 未设置（NULL）时表达式结果为 NULL，策略判否——**未注入即看不到任何行**
  （fail-closed）。这条是刻意的：不设"没声明就放行"的默认值。

对写入的影响（WITH CHECK 与 USING 同表达式）：
- 往不在授权范围内的项目写资源会被数据库拒绝（SQLSTATE 42501），
  应用层把它翻译为 403，而不是 500。
- 因此「创建项目」要求调用方持有租户内全部项目范围：新项目的 id 不在任何
  既有范围内，若允许项目级用户创建，他写进去自己也读不回来（自锁），
  而放宽 WITH CHECK 就等于允许往看不见的项目里写数据。

Revision ID: e7f8a9b0c1d2
Revises: d3e4f5a6b7c8
Create Date: 2026-11-16
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e7f8a9b0c1d2"
down_revision: str | Sequence[str] | None = "d3e4f5a6b7c8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# RLS 上下文键（与 packages/shared/rls.py 的常量必须一致）。
_ORG_SETTING = "app.current_org_id"
_SCOPE_SETTING = "app.current_projects"

# 表 -> 承载项目归属的列。
# projects 的「项目 id」就是它自己的主键 entity_id；其余表用外键列 project_id。
_SCOPED_TABLES: tuple[tuple[str, str, str], ...] = (
    ("project_planning", "projects", "entity_id"),
    ("project_planning", "episodes", "project_id"),
    ("project_planning", "script_revisions", "project_id"),
    ("asset_service", "asset_versions", "project_id"),
    ("asset_service", "qc_reports", "project_id"),
)


def _scope_expression(column: str) -> str:
    """构造归属判定表达式（USING 与 WITH CHECK 共用同一份定义）。

    两个维度**同时**成立才放行：

    1. 组织：``organization_id = 当前组织``。组织是客户内部的责任边界，
       跨组织连"全项目范围"也不放行——``*`` 的语义是「本组织内全部项目」，
       不是「本租户全部项目」。把 ``*`` 定义成租户级会让租户管理员天然
       越权到别的制作主体。
    2. 项目：``* 或 项目在授权集合内``。

    用 ``string_to_array`` 而不是把 id 拼进 SQL：GUC 的取值仍是参数化的文本，
    表达式本身不含任何用户数据。``NULLIF(..., '')`` 把「未设置」与「空串」
    统一归为 NULL，避免 ``''::text[]`` 这类隐式转换陷阱；GUC 未设置时整个
    表达式为 NULL，策略判否（fail-closed）。
    """
    return f"""(
        organization_id = NULLIF(current_setting('{_ORG_SETTING}', true), '')::uuid
        AND (
            current_setting('{_SCOPE_SETTING}', true) = '*'
            OR {column} = ANY (
                string_to_array(
                    NULLIF(current_setting('{_SCOPE_SETTING}', true), ''), ','
                )
            )
        )
    )"""


def upgrade() -> None:
    """为五张项目级资源表加限制性项目范围策略。"""
    for schema, table, column in _SCOPED_TABLES:
        expression = _scope_expression(column)
        op.execute(
            f"""
            CREATE POLICY project_scope_{table}
            ON {schema}.{table}
            AS RESTRICTIVE
            FOR ALL
            USING ({expression})
            WITH CHECK ({expression})
            """
        )


def downgrade() -> None:
    """回滚：只删本迁移新增的限制性策略（既有租户策略不受影响）。"""
    for schema, table, _column in _SCOPED_TABLES:
        op.execute(
            f"DROP POLICY IF EXISTS project_scope_{table} ON {schema}.{table}"
        )
