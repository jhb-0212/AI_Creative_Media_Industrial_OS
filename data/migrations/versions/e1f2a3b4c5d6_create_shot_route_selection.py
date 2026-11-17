"""生成执行域：shot_route_selections（镜头生效路线，单一路线生成）

Revision ID: e1f2a3b4c5d6
Revises: d0e1f2a3b4c5
Create Date: 2026-11-17

背景（Phase 1 验收 C4，ADR-R35）：
Phase 1 主链要求「单一路线生成」，但此前只有 ProductionRoute（平台能力目录
条目，字段集没有 project/shot 归属）与 GenerationTask——**没有任何对象承载
「某镜头当前生效路线」**。结果是：并发发起两条路线都会被接受，同一镜头也能在
两个路线上各受理一次生成。本迁移补上这个载体：

1. ``shot_route_selections`` 表：信封列 + project_id / shot_id / route_id。
2. **唯一约束** ``(tenant_id, project_id, shot_id)``：这是「单一路线」的
   结构事实。它必须由数据库强制——"选路前先查一下"在并发下必输：两个请求都
   查到"没有"，然后都插入成功。唯一约束让第二个必然失败，服务层再把失败
   翻译成结构化拒绝（ROUTE_ALREADY_SELECTED）。
3. **完全不可变**：生效路线是受理事实，不是可变配置。守卫触发器拒绝任何
   UPDATE 与 DELETE——允许原地改写等于允许绕过"第二条路线必须被拒绝"，
   历史也会变得不可信（"这个镜头到底走过哪条路线"将无法回答）。
4. 单值状态：``CHECK status = 'ACTIVE'``。没有转移表，因为不存在合法转移
   （变更路线属独立治理动作，动作矩阵无入口，不造不可达边）。
5. 授权：只授 SELECT / INSERT（应用角色连 UPDATE 权限都没有，触发器是第二
   道闸）；DELETE 一律不授——业务对象不可物理删除。

RLS 与 generation_tasks 同口径（租户隔离 + 项目范围限制性策略）：跨租户/跨项目
既查不到也写不进。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from packages.shared.db_roles import DatabaseDomain, role_for

# revision identifiers, used by Alembic.
revision: str = "e1f2a3b4c5d6"
down_revision: str | Sequence[str] | None = "d0e1f2a3b4c5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "generation_execution"
TABLE = "shot_route_selections"
OUTBOX_SCHEMA = "outbox"
ALEMBIC_VERSION = "public.alembic_version"

#: 单值状态集（迁移是历史快照，不 import 代码）。
SHOT_ROUTE_SELECTION_STATUS_VALUES = ("ACTIVE",)

#: 唯一约束名：同镜头只能有一条生效路线。
UNIQUE_NAME = "ux_shot_route_selection_effective"

_ORG_SETTING = "app.current_org_id"
_SCOPE_SETTING = "app.current_projects"


def _scope_expression(column: str) -> str:
    """项目归属判定表达式（与 generation_tasks 的策略文本保持同一口径）。"""
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


def _create_guard() -> None:
    """建不可变守卫（ADR-R20 范式的极端形态：可变列白名单为空）。

    与其它对象不同，这里**没有任何可变列**：生效路线没有"合法更新"这种情形。
    因此守卫不遍历列差异，直接拒绝 UPDATE——比"逐列比对后放行"更难写错。
    """
    op.execute(
        f"""
        CREATE FUNCTION {SCHEMA}.{TABLE}_guard()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION
                    '{SCHEMA}.{TABLE} 业务对象不可物理删除';
            END IF;
            RAISE EXCEPTION
                '{SCHEMA}.{TABLE} 为受理事实（生效路线），禁止改写'
                '（单一路线生成，V205-R14-A9）';
        END;
        $$
        """
    )
    op.execute(
        f"CREATE TRIGGER {TABLE}_guard_trigger "
        f"BEFORE UPDATE OR DELETE ON {SCHEMA}.{TABLE} "
        f"FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.{TABLE}_guard()"
    )


def upgrade() -> None:
    role = role_for(DatabaseDomain.GENERATION_EXECUTION)

    op.create_table(
        TABLE,
        # C.3.1 通用实体信封列
        sa.Column("entity_id", sa.String(64), primary_key=True),
        sa.Column("entity_type", sa.String(64), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("entity_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("owner_module", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.String(64), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("audit_ref", sa.String(128), nullable=True),
        # 业务列（契约 V205-R14-A9 冻结字段集）
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("shot_id", sa.String(64), nullable=False),
        sa.Column("route_id", sa.String(64), nullable=False),
        sa.CheckConstraint(
            "status IN ("
            + ", ".join(repr(v) for v in SHOT_ROUTE_SELECTION_STATUS_VALUES)
            + ")",
            name="ck_generation_execution_shot_route_selections_status_registered",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "shot_id",
            name=UNIQUE_NAME,
        ),
        schema=SCHEMA,
    )

    # 索引名与 SQLAlchemy 自动命名一致（EntityMixin 的 tenant_id /
    # organization_id 带 index=True，业务列 project_id 同范式）。
    op.create_index(
        f"ix_{SCHEMA}_{TABLE}_tenant_id", TABLE, ["tenant_id"], schema=SCHEMA
    )
    op.create_index(
        f"ix_{SCHEMA}_{TABLE}_organization_id",
        TABLE,
        ["organization_id"],
        schema=SCHEMA,
    )
    op.create_index(
        f"ix_{SCHEMA}_{TABLE}_project_id", TABLE, ["project_id"], schema=SCHEMA
    )

    _create_guard()

    # RLS：租户隔离 + 项目范围（限制性）策略（与 generation_tasks 同口径）。
    op.execute(f"ALTER TABLE {SCHEMA}.{TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY tenant_isolation_{TABLE}
        ON {SCHEMA}.{TABLE}
        USING (
            tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid
        )
        """
    )
    expression = _scope_expression("project_id")
    op.execute(
        f"""
        CREATE POLICY project_scope_{TABLE}
        ON {SCHEMA}.{TABLE}
        AS RESTRICTIVE
        FOR ALL
        USING ({expression})
        WITH CHECK ({expression})
        """
    )

    # 授权：只读 + 登记。不给 UPDATE（触发器的第二道闸在权限层已有第一道），
    # 不给 DELETE（业务对象不可物理删除）。审计事件需要 outbox 的 SELECT/INSERT。
    op.execute(f"GRANT SELECT, INSERT ON {SCHEMA}.{TABLE} TO {role}")
    op.execute(f"GRANT USAGE ON SCHEMA {OUTBOX_SCHEMA} TO {role}")
    op.execute(f"GRANT SELECT, INSERT ON {OUTBOX_SCHEMA}.outbox_events TO {role}")
    op.execute(f"GRANT SELECT ON {ALEMBIC_VERSION} TO {role}")


def downgrade() -> None:
    role = role_for(DatabaseDomain.GENERATION_EXECUTION)
    op.execute(f"REVOKE ALL ON {SCHEMA}.{TABLE} FROM {role}")
    op.execute(f"REVOKE ALL ON {OUTBOX_SCHEMA}.outbox_events FROM {role}")
    op.execute(f"REVOKE ALL ON {ALEMBIC_VERSION} FROM {role}")
    for policy in (f"project_scope_{TABLE}", f"tenant_isolation_{TABLE}"):
        op.execute(f"DROP POLICY IF EXISTS {policy} ON {SCHEMA}.{TABLE}")
    op.execute(f"ALTER TABLE {SCHEMA}.{TABLE} DISABLE ROW LEVEL SECURITY")
    op.execute(f"DROP TRIGGER IF EXISTS {TABLE}_guard_trigger ON {SCHEMA}.{TABLE}")
    op.execute(f"DROP FUNCTION IF EXISTS {SCHEMA}.{TABLE}_guard()")
    op.execute(f"DROP INDEX IF EXISTS {SCHEMA}.ix_{SCHEMA}_{TABLE}_project_id")
    op.execute(f"DROP INDEX IF EXISTS {SCHEMA}.ix_{SCHEMA}_{TABLE}_organization_id")
    op.execute(f"DROP INDEX IF EXISTS {SCHEMA}.ix_{SCHEMA}_{TABLE}_tenant_id")
    op.drop_table(TABLE, schema=SCHEMA)
