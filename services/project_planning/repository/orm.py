"""Project ORM 模型（映射到独立 schema：project_planning）。

数据所有权（14.2）：
- project_planning 模块拥有 Project / Episode / ScriptRevision 表的写入权。
- 使用独立 schema「project_planning」，避免与其它模块耦合。
- 表结构严格对齐 C.3.1 通用实体信封字段（信封列收敛到 EntityMixin）。

设计要点（消除重复机制 R4）：
- 三个模型重复声明的 C.3.1 信封列（entity_id/entity_type/schema_version/
  entity_version/status/owner_module/created_at/created_by/updated_at/audit_ref）
  收敛到 repository/entity_mixin.py 的 EntityMixin。
- 各模型的业务列（project_name/project_tier/title 等）保留在各自类中。
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    DateTime,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from packages.shared.database import Base
from packages.shared.orm import EntityMixin

# 独立 schema 名称（数据所有权边界）。
SCHEMA_NAME = "project_planning"


class OrganizationModel(Base):
    """organizations 表模型（归属根，纯归属/隔离键）。

    定位：organization 是 Tenant → Organization → Project 归属链的中间层，
    是**纯归属键**而非业务聚合根——无 entity_type、无状态机、无对象 schema，
    与 tenant_id 同构。字段最小化：organization_id 主键 + tenant_id + name。

    数据所有权：本表属 project_planning 域（当前唯一消费方是项目域），
    写入权归该域；未来资产域若需归属组织，再按方案 3.3 数据所有权边界演进。
    """

    __tablename__ = "organizations"
    __table_args__ = (
        # 复合外键的被引用键：projects 以 (organization_id, tenant_id) 引用本表，
        # 使「项目引用的组织必须与项目同租户」在数据库层成立（审查 P1-03）。
        UniqueConstraint(
            "organization_id",
            "tenant_id",
            name="uq_organizations_owner_tenant",
        ),
        {"schema": SCHEMA_NAME},
    )

    organization_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class ProjectModel(EntityMixin, Base):
    """projects 表模型。

    归属完整性（决策 2 / 外部审查 F04 / 审查 P1-03）：
    - organization_id 外键指向 organizations，可空（个人租户无组织）；非空时
      在数据库层锁死「项目必须归属真实组织」。
    - 外键是**租户复合外键** (organization_id, tenant_id) →
      organizations(organization_id, tenant_id)：单列外键只能证明组织存在，
      无法阻止「项目属租户 A、组织属租户 B」的跨租户错配（P1-03）。复合外键
      把「同一租户」这条归属不变量放到数据库层；organization_id 为 NULL 时
      MATCH SIMPLE 语义下不校验，与「个人租户不强制归属」一致。
    - organization_id 是内部归属键，不进 API 响应（与 tenant_id 一致）。
    - ``uq_projects_entity_tenant``：跨域复合外键的**引用目标**。asset_service 的
      ``asset_versions(project_id, tenant_id)`` 必须指向唯一键，否则复合外键无法建立。
      它必须声明在模型里——只在迁移里建、模型不写，`alembic check` 就会把它当成
      「待删除的约束」，下一个人「修 CI」时删掉它，跨域归属外键随之失效。
    """

    __tablename__ = "projects"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "tenant_id"],
            [
                f"{SCHEMA_NAME}.organizations.organization_id",
                f"{SCHEMA_NAME}.organizations.tenant_id",
            ],
            name="fk_projects_organization_tenant",
        ),
        UniqueConstraint(
            "entity_id",
            "tenant_id",
            name="uq_projects_entity_tenant",
        ),
        {"schema": SCHEMA_NAME},
    )

    # 业务字段
    project_name: Mapped[str] = mapped_column(String(200), nullable=False)
    project_tier: Mapped[str] = mapped_column(String(8), nullable=False)
    # 归属键：组织归属（可空 = 个人租户）。
    organization_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)


class ScriptModel(EntityMixin, Base):
    """script_revisions 表模型（剧本版本，同属 project_planning schema）。

    归属完整性（决策 2）：
    - 复合外键 ``(project_id, episode_ref) → episodes(project_id, episode_ref)``
      在数据库层锁死「跨项目乱挂载」——任何写入的 (project_id, episode_ref)
      必须精确匹配一个真实存在的 Episode，杜绝仅靠 Service 查询校验的
      归属漏洞（R2 不变量放最可靠层级）。
    - 部分唯一索引 ux_script_active_version 兜底并发取代（见迁移 facd8d42d07f）。
    """

    __tablename__ = "script_revisions"

    # 业务字段（project_id + episode_ref 是复合外键的自然键列，非裸冗余）。
    project_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    episode_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    # 锁定申请人（LOCK_PENDING 态写入，批准动作做职责分离比对）。
    requested_by: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # 部分唯一索引：同项目 + 同集数下最多一个「非 SUPERSEDED」活跃版本。
    __table_args__ = (
        Index(
            "ux_script_active_version",
            "project_id",
            "episode_ref",
            unique=True,
            postgresql_where="status <> 'SUPERSEDED'",
        ),
        # 复合外键：强绑定 episodes(project_id, episode_ref)，防跨项目乱挂载。
        ForeignKeyConstraint(
            ["project_id", "episode_ref"],
            [f"{SCHEMA_NAME}.episodes.project_id", f"{SCHEMA_NAME}.episodes.episode_ref"],
            name="fk_script_revisions_episode",
        ),
        {"schema": SCHEMA_NAME},
    )


class EpisodeModel(EntityMixin, Base):
    """episodes 表模型（集数，归属 Project）。

    归属完整性（决策 2 / 外部审查 F04）：
    - ``(project_id, episode_ref)`` 唯一约束（自然键），是 script_revisions
      复合外键的引用目标，保证「同项目内 episode_ref 唯一、跨项目允许同名」。
    - ``project_id`` 外键指向 projects(entity_id)，在数据库层锁死「集数必须
      归属真实存在的项目」，不再仅靠 Service 查询校验（R2 不变量放最可靠层）。
    """

    __tablename__ = "episodes"
    __table_args__ = (
        # 自然键唯一约束：同项目内 episode_ref 唯一（复合外键的引用目标）。
        UniqueConstraint(
            "project_id",
            "episode_ref",
            name="ux_episode_ref_per_project",
        ),
        # 归属外键：episodes.project_id -> projects.entity_id。
        ForeignKeyConstraint(
            ["project_id"],
            [f"{SCHEMA_NAME}.projects.entity_id"],
            name="fk_episodes_project",
        ),
        {"schema": SCHEMA_NAME},
    )

    # 业务字段
    project_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    episode_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
