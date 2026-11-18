"""资产归属项目：asset_versions.project_id + 跨 schema 复合外键（SCOPE-3）

根因（第二轮重新审查 P1-04）
------------------------------------------------------------
``AssetVersionModel`` 只承载 ``tenant_id``，没有 project_id / organization_id，
而 ``action_registry.yaml`` 对 ASSET.* 动作声明 ``scope: project/shot/asset``。
契约声明的作用域在模型里无法表达，导致：

* 项目级/组织级资产授权无从判定——只能退化成「同租户即可见」，
  而租户 RLS 明确不承担组织隔离（SCOPE-1 已确立组织是租户内的一级边界）；
* 资产无法被「按项目列举」（镜头/生成/交付主链都需要），业务侧只能靠
  ``source_asset_version_ids`` 或 storage_ref 里的路径字符串猜归属。

根治方式（不是给模型加一个字段就算解决）
------------------------------------------------------------
1. **归属唯一**：project_id 是资产唯一的归属键，NOT NULL + 复合外键
   ``(project_id, tenant_id) -> project_planning.projects(entity_id, tenant_id)``。
   复合而非单列，是因为单列外键只能证明「项目存在」，不能证明「项目与资产同
   租户」——SCOPE-1 的审查结论在此同样适用，不能重犯。
2. **组织不落库**：organization 由 project 派生。冗余一列会在「项目改归属组织」
   时产生第二事实源（第三轮审查指认的 audit_ref 式漂移面）。
3. **镜头是引用不是归属**：``scope: project/shot/asset`` 描述授权作用域，
   不是归属层级。镜头级关系等 Shot 域落地时以引用表表达；现在用多态归属列
   冒充，反而会得到「无法建外键」的假归属。
4. **跨域只读归属键**：asset_service_role 获得 projects 上
   ``(entity_id, tenant_id, organization_id)`` 的**列级 SELECT**，
   仅够解析归属链，拿不到项目业务字段，也绝无写权限。

迁移安全性
------------------------------------------------------------
project_id 是 NOT NULL 且无合理默认值：存量资产无法自动推断归属。因此本迁移
**fail-closed**——表非空时直接失败并要求人工决策（迁移回填不是可以猜的事）。
Phase 1 尚未验收、资产表在生产为空，这条限制不会阻塞发布；
一旦有真实数据，归属必须由人给出，而不是由迁移脚本编造。

Revision ID: b3c4d5e6f7a8
Revises: d2e3f4a5b6c7
Create Date: 2026-11-18
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b3c4d5e6f7a8"
down_revision: str | Sequence[str] | None = "d2e3f4a5b6c7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ASSET_SCHEMA = "asset_service"
PROJECT_SCHEMA = "project_planning"
PROJECT_TABLE = f"{PROJECT_SCHEMA}.projects"
ASSET_TABLE = f"{ASSET_SCHEMA}.asset_versions"
FK_NAME = "fk_asset_versions_project_tenant"
INDEX_NAME = "ix_asset_service_asset_versions_project"
PROJECT_TENANT_UNIQUE = "uq_projects_entity_tenant"

# 归属链解析只需要这三列：主键、租户键、组织键。多授一列就多一分越界面。
OWNERSHIP_COLUMNS = ("entity_id", "tenant_id", "organization_id")


def _asset_service_role() -> str:
    """域角色名与运行时 DSN 同源（settings），不硬编码。"""
    from packages.shared.config import get_settings

    return get_settings().postgres_asset_service_user


def upgrade() -> None:
    bind = op.get_bind()

    # 1. 存量守卫：表非空即拒绝（归属不能由迁移猜）。
    existing = bind.execute(sa.text(f"SELECT count(*) FROM {ASSET_TABLE}")).scalar_one()
    if existing:
        raise RuntimeError(
            f"{ASSET_TABLE} 已有 {existing} 条记录，无法自动推断 project_id。"
            "资产归属是业务事实，必须由人工给出映射后再迁移——"
            "本迁移拒绝编造归属（fail-closed）。"
        )

    # 2. 被引用键：projects(entity_id, tenant_id) 必须唯一（复合外键的引用目标）。
    op.create_unique_constraint(
        PROJECT_TENANT_UNIQUE,
        "projects",
        ["entity_id", "tenant_id"],
        schema=PROJECT_SCHEMA,
    )

    # 3. 归属列 + 复合外键 + 查询索引。
    op.add_column(
        "asset_versions",
        sa.Column("project_id", sa.String(length=64), nullable=False),
        schema=ASSET_SCHEMA,
    )
    op.create_foreign_key(
        FK_NAME,
        "asset_versions",
        "projects",
        ["project_id", "tenant_id"],
        ["entity_id", "tenant_id"],
        source_schema=ASSET_SCHEMA,
        referent_schema=PROJECT_SCHEMA,
    )
    op.create_index(
        INDEX_NAME,
        "asset_versions",
        ["project_id"],
        schema=ASSET_SCHEMA,
    )

    # 4. 归属链只读投影：列级 SELECT，不给整表、不给写。
    role = _asset_service_role()
    columns = ", ".join(OWNERSHIP_COLUMNS)
    op.execute(f"GRANT USAGE ON SCHEMA {PROJECT_SCHEMA} TO {role}")
    op.execute(f"GRANT SELECT ({columns}) ON {PROJECT_TABLE} TO {role}")


def downgrade() -> None:
    role = _asset_service_role()
    columns = ", ".join(OWNERSHIP_COLUMNS)
    op.execute(f"REVOKE SELECT ({columns}) ON {PROJECT_TABLE} FROM {role}")

    op.drop_index(INDEX_NAME, table_name="asset_versions", schema=ASSET_SCHEMA)
    op.drop_constraint(
        FK_NAME, "asset_versions", schema=ASSET_SCHEMA, type_="foreignkey"
    )
    op.drop_column("asset_versions", "project_id", schema=ASSET_SCHEMA)
    op.drop_constraint(
        PROJECT_TENANT_UNIQUE, "projects", schema=PROJECT_SCHEMA, type_="unique"
    )
    # schema USAGE 不回收：project_planning_role / 其他域可能同样依赖它，
    # 回收是权限矩阵层面的决策，不属于本迁移的回滚语义。
