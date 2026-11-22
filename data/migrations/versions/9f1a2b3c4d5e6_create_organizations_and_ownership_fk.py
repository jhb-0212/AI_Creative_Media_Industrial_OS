"""create organizations + project/episode ownership foreign keys

根因（外部审查 F04 + 第三轮审查「数据模型无约束」）：
- Project 无 organization_id，无 organization 表，归属链
  Tenant → Organization → Project 完全缺失；同一租户下不同组织/项目的
  主体因 tenant_id 相同即可互访，缺项目级资源授权隔离。
- Episode.project_id 仅有普通索引，无指向 projects 的外键；Episode 归属
  Project 的正确性仍依赖 Service 层查询校验（R2 不变量未放最可靠层）。

改动（本迁移是归属链的物理落地，非业务扩展）：
1. 新建 organizations 表（归属根，最小字段集：organization_id 主键 +
   tenant_id + name）。organization 是纯归属/隔离键，非业务聚合根，
   因此无 entity_type/状态机/对象 schema——与 tenant_id 同构。
2. projects 加 organization_id（可空：个人租户无组织；非空：组织项目）+
   外键 -> organizations(organization_id)。
3. episodes 加外键 project_id -> projects(entity_id)，在数据库层锁死
   「集数必须归属真实存在的项目」。

设计要点（不变量放最可靠层）：
- organization 放 project_planning schema：当前唯一消费方是项目域，写入权
  归该域；未来若资产域也要归属组织，再按数据所有权边界演进（见方案 3.3）。
- organization_id 采用 UUID 主键（与 entity_id 一致的 8.2 规则：主键 UUID）。
- organization_id 是内部归属键，不进 API 响应契约（与 tenant_id 一致：
  object_schemas 全文无 tenant_id，_envelope_dict 不输出 tenant_id）。
- projects.organization_id 可空：个人租户场景组织未建，不强制归属。

Revision ID: 9f1a2b3c4d5e6
Revises: a1b2c3d4e5f6
Create Date: 2026-11-17
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9f1a2b3c4d5e6"
down_revision: str | Sequence[str] | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "project_planning"


def upgrade() -> None:
    """新建 organizations 表 + projects.organization_id 外键 + episodes 外键。"""
    # 1. 建 organizations 表（归属根，最小字段集）。
    op.create_table(
        "organizations",
        sa.Column("organization_id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False, index=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.String(64), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        schema=SCHEMA,
    )

    # 2. projects 加 organization_id（可空，先加列再建外键）。
    op.add_column(
        "projects",
        sa.Column("organization_id", sa.Uuid(), nullable=True),
        schema=SCHEMA,
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

    # 3. episodes 加外键 project_id -> projects(entity_id)。
    #    project_id 存的就是 projects.entity_id（见 EpisodeService 归属验证：
    #    project_id -> ProjectRepository.get_by_id(entity_id)）。
    op.create_foreign_key(
        "fk_episodes_project",
        "episodes",
        "projects",
        ["project_id"],
        ["entity_id"],
        source_schema=SCHEMA,
        referent_schema=SCHEMA,
    )


def downgrade() -> None:
    """回滚：删外键 + 删 organization_id 列 + 删 organizations 表。"""
    op.drop_constraint(
        "fk_episodes_project", "episodes", schema=SCHEMA, type_="foreignkey"
    )
    op.drop_constraint(
        "fk_projects_organization",
        "projects",
        schema=SCHEMA,
        type_="foreignkey",
    )
    op.drop_column("projects", "organization_id", schema=SCHEMA)
    op.drop_table("organizations", schema=SCHEMA)
