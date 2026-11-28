"""组织归属租户复合约束 + organizations RLS（审查 P1-03 / Review Thread R8）

根因（归属约束只到「组织存在」，不到「组织属于同一租户」）：
- 迁移 9f1a2b3c4d5e6 建了 ``projects.organization_id ->
  organizations(organization_id)`` 单列外键，但未纳入 tenant_id。后果：
  项目属租户 A、组织属租户 B 时，项目仍能引用租户 B 的组织——数据库只能
  证明「组织存在」，不能证明「组织归属正确」。跨租户归属错配在数据库层
  无法被拒绝，仍依赖 Service 层查询校验（R2 不变量未放最可靠层）。
- organizations 表本身未启用 RLS：projects/episodes/script_revisions 三表
  有租户隔离，但归属根 organizations 可被 ``SELECT *`` 跨租户读出全部组织
  清单，隔离链在「归属根」处断裂。

根治（把归属不变量下沉到数据库约束层，非代码校验）：
1. organizations 加租户复合唯一约束 ``UNIQUE (organization_id, tenant_id)``，
   作为复合外键的被引用键。
2. projects 的 organization 外键升级为复合外键
   ``(organization_id, tenant_id) -> organizations(organization_id, tenant_id)``：
   项目引用的组织必须与其同租户（tenant_id 一致），跨租户引用在数据库层被拒。
   项目 organization_id 可空（个人租户无组织），复合外键 MATCH SIMPLE 语义下
   任一列为 NULL 即不校验，与「个人租户不强制归属」一致。
3. organizations 启用 RLS + 租户隔离策略（与三张核心表同构），归属根不再
   跨租户裸露。

Revision ID: d2e3f4a5b6c7
Revises: c1f2e3d4a5b6
Create Date: 2026-11-18
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d2e3f4a5b6c7"
down_revision: str | Sequence[str] | None = "c1f2e3d4a5b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "project_planning"


def upgrade() -> None:
    """organizations 复合唯一约束 + projects 复合外键 + organizations RLS。"""
    # 1. 复合唯一约束（复合外键的被引用键）。organization_id 本身已是主键
    #    （唯一），此处额外约束 (organization_id, tenant_id) 唯一——逻辑上
    #    主键已蕴含，但复合外键要求被引用列有精确匹配的 UNIQUE/主键，故显式
    #    声明（PG 要求复合外键引用「由 UNIQUE 覆盖的列组合」）。
    op.create_unique_constraint(
        "uq_organizations_owner_tenant",
        "organizations",
        ["organization_id", "tenant_id"],
        schema=SCHEMA,
    )

    # 2. projects 的 organization 外键升级为复合外键（含 tenant_id）。
    #    先删旧的单列外键 fk_projects_organization，再建复合外键。
    op.drop_constraint("fk_projects_organization", "projects", schema=SCHEMA, type_="foreignkey")
    op.create_foreign_key(
        "fk_projects_organization_tenant",
        "projects",
        "organizations",
        ["organization_id", "tenant_id"],
        ["organization_id", "tenant_id"],
        source_schema=SCHEMA,
        referent_schema=SCHEMA,
    )

    # 3. organizations 启用 RLS + 租户隔离策略（与 projects/episodes/
    #    script_revisions 同构，归属根不再跨租户裸露）。
    op.execute(f"ALTER TABLE {SCHEMA}.organizations ENABLE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY tenant_isolation_organizations
        ON {SCHEMA}.organizations
        USING (
            tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid
        )
        """
    )


def downgrade() -> None:
    """回滚：删 RLS + 复合外键 + 复合唯一约束，恢复单列外键。"""
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation_organizations ON {SCHEMA}.organizations")
    op.execute(f"ALTER TABLE {SCHEMA}.organizations DISABLE ROW LEVEL SECURITY")

    op.drop_constraint(
        "fk_projects_organization_tenant",
        "projects",
        schema=SCHEMA,
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_projects_organization",
        "projects",
        "organizations",
        ["organization_id"],
        ["organization_id"],
        source_schema=SCHEMA,
        referent_schema=SCHEMA,
    )
    op.drop_constraint(
        "uq_organizations_owner_tenant", "organizations", schema=SCHEMA, type_="unique"
    )
