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
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from packages.shared.database import Base
from packages.shared.orm import EntityMixin
from packages.shared.status_constraint import status_check
from services.project_planning.domain.enums import (
    EpisodeStatus,
    ProjectStatus,
    SceneStatus,
    ScriptStatus,
    ShotRevisionStatus,
    ShotStatus,
)

# 独立 schema 名称（数据所有权边界）。
SCHEMA_NAME = "project_planning"
# 镜头域表在独立 schema；运行时服务通过端口写入，模块边界不因此变成第二套真相。
SHOT_SCHEMA_NAME = "shot_planning"


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
        # status 收口到契约状态集：契约把 status 收成枚举之后，库里若仍接受任意
        # 字符串，「未知枚举值必须拒绝」（C.3.1）就只在你记得走 Service 时成立。
        # 值列表由 packages.shared.status_constraint 从 ProjectStatus 生成，
        # 与契约同一出处，闸门会逐字比对。
        status_check((s.value for s in ProjectStatus), table="projects"),
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
        # status 收口（DRAFT / LOCK_PENDING / LOCKED / SUPERSEDED）。
        status_check((s.value for s in ScriptStatus), table="script_revisions"),
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
        # status 收口（DRAFT / ACTIVE / ARCHIVED）。
        status_check((s.value for s in EpisodeStatus), table="episodes"),
        {"schema": SCHEMA_NAME},
    )

    # 业务字段
    project_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    episode_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)


class SceneModel(EntityMixin, Base):
    """scenes 表模型（场次，归属 Episode，作用域收敛到 Project）。

    表结构与批次五（``i9d0e1f2a3b4``）逐列一致；这里补 ORM 映射不是「第二份
    事实源」，而是让领域模型与持久化模型可互换——两者的列清单必须一致，
    否则 ``alembic check`` 会把真实存在的约束当成待删除项，下一个人「修 CI」
    时删掉它，归属外键随之失效（``uq_projects_entity_tenant`` 已有同类教训）。

    归属完整性（三层，缺一层都留一个可绕的洞）：
    - ``fk_scenes_project_tenant_org``：三列外键，锁死「场次的项目必须是同租户
      同组织的真实项目」。单列 ``project_id`` 只能证明项目存在。
    - ``fk_scenes_episode_project``：``(episode_id, project_id) → episodes``，
      锁死「场次挂的集必须属于同一个项目」——跨项目挂载在数据库层被拒。
    - ``uq_scenes_project_code``：项目内场次编号唯一；它是展示编号，但重复的
      展示编号会让「按编号沟通」变成运气问题。
    """

    __tablename__ = "scenes"
    __table_args__ = (
        UniqueConstraint("scene_id", name="uq_scenes_identity"),
        # 信封主键与业务身份必须同值（契约 C.3.1）。领域模型用 property 表达
        # 同一件事，此处是数据库侧的最后一道：绕过应用层的写入也会被拒。
        CheckConstraint("entity_id = scene_id", name="ck_scenes_single_identity"),
        # 镜头侧复合外键的被引用键：shots(project_id, scene_id) → scenes。
        UniqueConstraint("project_id", "scene_id", name="uq_scenes_project_scene"),
        UniqueConstraint("project_id", "scene_code", name="uq_scenes_project_code"),
        ForeignKeyConstraint(
            ["project_id", "tenant_id", "organization_id"],
            [
                f"{SCHEMA_NAME}.projects.entity_id",
                f"{SCHEMA_NAME}.projects.tenant_id",
                f"{SCHEMA_NAME}.projects.organization_id",
            ],
            name="fk_scenes_project_tenant_org",
        ),
        ForeignKeyConstraint(
            ["episode_id", "project_id"],
            [
                f"{SCHEMA_NAME}.episodes.entity_id",
                f"{SCHEMA_NAME}.episodes.project_id",
            ],
            name="fk_scenes_episode_project",
        ),
        status_check((s.value for s in SceneStatus), table="scenes"),
        {"schema": SCHEMA_NAME},
    )

    # 业务字段
    scene_id: Mapped[str] = mapped_column(String(64), nullable=False)
    scene_code: Mapped[str] = mapped_column(String(64), nullable=False)
    # 归属锚点：Scene 唯一的上行引用（集），配合 project_id 构成复合外键。
    episode_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # 组织归属键（内部键，不进 API 响应）。NOT NULL：场景必须有组织归属，
    # 由服务层从父项目拷贝，调用方不得自填。
    organization_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)


class ShotModel(EntityMixin, Base):
    """shots 表（稳定逻辑镜头，只承载身份、展示编号与粗粒度进度）。

    约束形状与批次五迁移逐条对应；这里声明它们是让 ORM 成为可校验的投影，
    而不是让「数据库有、模型没有」的约束在下一次 `alembic check` 时被误删。
    """

    __tablename__ = "shots"
    __table_args__ = (
        UniqueConstraint("shot_id", name="uq_shots_identity"),
        CheckConstraint("entity_id = shot_id", name="ck_shots_single_identity"),
        UniqueConstraint("project_id", "shot_code", name="uq_shots_project_code"),
        UniqueConstraint("project_id", "shot_id", name="uq_shots_project_shot"),
        status_check((s.value for s in ShotStatus), table="shots"),
        CheckConstraint(
            "(status = 'BLOCKED' AND blocked_reason IS NOT NULL) OR "
            "(status <> 'BLOCKED' AND blocked_reason IS NULL)",
            name="ck_shots_blocked_reason",
        ),
        CheckConstraint(
            "(status = 'CANCELLED' AND cancellation_ref IS NOT NULL AND cancelled_at IS NOT NULL) OR "
            "(status <> 'CANCELLED' AND cancellation_ref IS NULL AND cancelled_at IS NULL)",
            name="ck_shots_cancelled",
        ),
        ForeignKeyConstraint(
            ["project_id", "tenant_id", "organization_id"],
            [
                f"{SCHEMA_NAME}.projects.entity_id",
                f"{SCHEMA_NAME}.projects.tenant_id",
                f"{SCHEMA_NAME}.projects.organization_id",
            ],
            name="fk_shots_project_tenant_org",
        ),
        ForeignKeyConstraint(
            ["project_id", "scene_id"],
            [f"{SCHEMA_NAME}.scenes.project_id", f"{SCHEMA_NAME}.scenes.scene_id"],
            name="fk_shots_scene_project",
        ),
        ForeignKeyConstraint(
            ["project_id", "rework_basis_ref"],
            ["workflow.rework_tasks.project_id", "workflow.rework_tasks.rework_task_id"],
            name="fk_shots_rework_basis",
        ),
        # 与首版修订互为引用，故必须可延迟；理由见批次五迁移注释。
        ForeignKeyConstraint(
            ["shot_revision_id", "project_id", "tenant_id", "organization_id"],
            [
                f"{SHOT_SCHEMA_NAME}.shot_revisions.entity_id",
                f"{SHOT_SCHEMA_NAME}.shot_revisions.project_id",
                f"{SHOT_SCHEMA_NAME}.shot_revisions.tenant_id",
                f"{SHOT_SCHEMA_NAME}.shot_revisions.organization_id",
            ],
            name="fk_shots_current_revision",
            deferrable=True,
            initially="DEFERRED",
        ),
        {"schema": SHOT_SCHEMA_NAME},
    )

    shot_id: Mapped[str] = mapped_column(String(64), nullable=False)
    shot_code: Mapped[str] = mapped_column(String(64), nullable=False)
    scene_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    shot_revision_id: Mapped[str] = mapped_column(String(64), nullable=False)
    blocked_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    rework_basis_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    cancellation_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    source_draft_ref: Mapped[dict[str, object] | None] = mapped_column(
        JSONB, nullable=True
    )
    organization_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)


class ShotRevisionModel(EntityMixin, Base):
    """shot_revisions 表（不可变内容版本，批次十后与契约 required 对齐）。"""

    __tablename__ = "shot_revisions"
    __table_args__ = (
        UniqueConstraint("shot_revision_id", name="uq_shot_revision_identity"),
        CheckConstraint(
            "entity_id = shot_revision_id", name="ck_shot_revisions_single_identity"
        ),
        # 四列唯一键：三条真实引用边的复合外键目标（批次十一）。
        UniqueConstraint(
            "entity_id",
            "project_id",
            "tenant_id",
            "organization_id",
            name="uq_shot_revisions_entity_project_tenant_org",
        ),
        status_check((s.value for s in ShotRevisionStatus), table="shot_revisions"),
        CheckConstraint(
            "confirmed_by = 'LEGACY_BACKFILL' OR basis_script_revision_id IS NOT NULL",
            name="ck_sr_basis_script",
        ),
        CheckConstraint(
            "(constraint_pack_applicability = 'BOUND' AND constraint_pack_id IS NOT NULL) OR "
            "(constraint_pack_applicability = 'NOT_APPLICABLE' AND constraint_pack_id IS NULL)",
            name="ck_sr_constraint_pack",
        ),
        ForeignKeyConstraint(
            ["project_id", "tenant_id", "organization_id"],
            [
                f"{SCHEMA_NAME}.projects.entity_id",
                f"{SCHEMA_NAME}.projects.tenant_id",
                f"{SCHEMA_NAME}.projects.organization_id",
            ],
            name="fk_shot_revisions_project_tenant_org",
        ),
        ForeignKeyConstraint(
            ["project_id", "episode_id"],
            [f"{SCHEMA_NAME}.episodes.project_id", f"{SCHEMA_NAME}.episodes.entity_id"],
            name="fk_shot_revisions_episode_identity",
        ),
        ForeignKeyConstraint(
            ["project_id", "scene_id"],
            [f"{SCHEMA_NAME}.scenes.project_id", f"{SCHEMA_NAME}.scenes.scene_id"],
            name="fk_shot_revisions_scene_project",
        ),
        ForeignKeyConstraint(
            ["project_id", "shot_id"],
            [f"{SHOT_SCHEMA_NAME}.shots.project_id", f"{SHOT_SCHEMA_NAME}.shots.shot_id"],
            name="fk_shot_revisions_shot_project",
            deferrable=True,
            initially="DEFERRED",
        ),
        ForeignKeyConstraint(
            ["basis_script_revision_id", "project_id", "tenant_id", "organization_id"],
            [
                f"{SCHEMA_NAME}.script_revisions.entity_id",
                f"{SCHEMA_NAME}.script_revisions.project_id",
                f"{SCHEMA_NAME}.script_revisions.tenant_id",
                f"{SCHEMA_NAME}.script_revisions.organization_id",
            ],
            name="fk_sr_basis_script",
        ),
        ForeignKeyConstraint(
            ["constraint_pack_id", "project_id", "tenant_id", "organization_id"],
            [
                f"{SHOT_SCHEMA_NAME}.constraint_packs.entity_id",
                f"{SHOT_SCHEMA_NAME}.constraint_packs.project_id",
                f"{SHOT_SCHEMA_NAME}.constraint_packs.tenant_id",
                f"{SHOT_SCHEMA_NAME}.constraint_packs.organization_id",
            ],
            name="fk_sr_constraint_pack",
        ),
        ForeignKeyConstraint(
            ["supersedes_shot_revision_id", "project_id", "tenant_id", "organization_id"],
            [
                f"{SHOT_SCHEMA_NAME}.shot_revisions.entity_id",
                f"{SHOT_SCHEMA_NAME}.shot_revisions.project_id",
                f"{SHOT_SCHEMA_NAME}.shot_revisions.tenant_id",
                f"{SHOT_SCHEMA_NAME}.shot_revisions.organization_id",
            ],
            name="fk_sr_supersedes",
        ),
        {"schema": SHOT_SCHEMA_NAME},
    )

    shot_revision_id: Mapped[str] = mapped_column(String(64), nullable=False)
    shot_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    episode_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    scene_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    episode_id: Mapped[str] = mapped_column(String(64), nullable=False)
    scene_id: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    narrative_intent: Mapped[str] = mapped_column(Text, nullable=False)
    character_refs: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    location_ref: Mapped[str] = mapped_column(String(256), nullable=False)
    prop_refs: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    constraint_pack_applicability: Mapped[str] = mapped_column(String(32), nullable=False)
    constraint_pack_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    creative_autonomy: Mapped[str] = mapped_column(String(8), nullable=False)
    risk: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    quality_profile: Mapped[str] = mapped_column(String(32), nullable=False)
    intent_ref: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    basis_script_revision_id: Mapped[str] = mapped_column(String(64), nullable=False)
    script_anchors: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    source_draft_ref: Mapped[dict[str, object] | None] = mapped_column(
        JSONB, nullable=True
    )
    source_candidate_refs: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    change_origin: Mapped[str] = mapped_column(String(32), nullable=False)
    supersedes_shot_revision_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    change_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    changed_fields: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    provenance: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    confirmed_by: Mapped[str] = mapped_column(String(64), nullable=False)
    confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False)
    organization_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
