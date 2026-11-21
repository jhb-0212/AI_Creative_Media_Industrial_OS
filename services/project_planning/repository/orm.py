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
from typing import Any
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
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from packages.shared.database import Base
from packages.shared.orm import EntityMixin
from packages.shared.status_constraint import status_check
from services.project_planning.domain.constraint_pack import LOCKED_DIMENSIONS
from services.project_planning.domain.enums import (
    ConstraintPackStatus,
    EpisodeStatus,
    ProjectStatus,
    ReworkTaskStatus,
    SceneStatus,
    ScriptStatus,
    ShotDraftStatus,
    ShotRevisionStatus,
    ShotSpecStatus,
    ShotStatus,
)

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


class SceneModel(EntityMixin, Base):
    """scenes 表模型（场次，归属 Episode）。

    归属完整性（A13 / ADR-R28 §6）：
    - 复合外键 (project_id, episode_id) → episodes(project_id, entity_id)：单列外键
      只能证明集数存在，无法阻止「场次属项目 A、集数属项目 B」的跨项目错配。
    - (project_id, scene_code) 唯一：契约 SC-3 要求「scene_code 在项目内唯一」，
      因此唯一域是**项目**而不是集数。早先实现成 (project_id, episode_id,
      scene_code) 是弱于契约的口径（跨集可重名），已按契约收敛。编号语义虽是
      按集编排，但唯一性必须按契约的项目口径裁决——否则「项目内引用某个
      scene_code」会有多个解。
    - CHECK (scene_id = entity_id)：契约 SC-1「身份单一」。领域层已强校验，
      但那只能挡住经过 Service 的写入；CHECK 让绕过服务直连 DB 的写入也失败。
    """

    __tablename__ = "scenes"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "entity_id",
            name="uq_scenes_entity_project",
        ),
        UniqueConstraint(
            "project_id",
            "scene_code",
            name="uq_scene_code_per_project",
        ),
        ForeignKeyConstraint(
            ["project_id", "episode_id"],
            [
                f"{SCHEMA_NAME}.episodes.project_id",
                f"{SCHEMA_NAME}.episodes.entity_id",
            ],
            name="fk_scenes_episode",
        ),
        CheckConstraint("scene_id = entity_id", name="ck_scenes_identity_single"),
        status_check((s.value for s in SceneStatus), table="scenes"),
        {"schema": SCHEMA_NAME},
    )

    scene_id: Mapped[str] = mapped_column(String(64), nullable=False)
    scene_code: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    episode_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)


class ShotModel(EntityMixin, Base):
    """shots 表模型（镜头，归属 Scene）。

    四个证据列不是可选项：Shot 的 10 条边里 6 条带守卫，没有它们守卫无处落地。
    库层 CHECK 与 ``object_schemas`` 里 Shot 的 statement 逐字对应；领域层仍校验，
    两层分别负责「错误可读」与「绕过服务也写不进去」。

    三条与契约逐字对应的库层约束：
    - ``uq_shot_code_per_project``：SH-3 要求「shot_code 在项目内唯一」，唯一域是
      项目而非场次（早先实现成 (project_id, scene_id, shot_code) 弱于契约）。
    - ``ck_shots_identity_single``：SH-1 身份单一，挡住绕过服务直连 DB 的写入。
    - ``ck_shots_evidence_consistency``：SH-5 状态证据自洽。
    """

    __tablename__ = "shots"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "entity_id",
            name="uq_shots_entity_project",
        ),
        UniqueConstraint(
            "project_id",
            "shot_code",
            name="uq_shot_code_per_project",
        ),
        ForeignKeyConstraint(
            ["project_id", "scene_id"],
            [f"{SCHEMA_NAME}.scenes.project_id", f"{SCHEMA_NAME}.scenes.entity_id"],
            name="fk_shots_scene",
        ),
        # 复合外键：跨项目借用别人的返工授权必须被数据库拒绝，不能靠 Service
        # 记得比 project_id。目标键 uq_rework_tasks_entity_project 由同文件
        # ReworkTaskModel 声明；存量库的升级走 b1c2d3e4f5a6 迁移。
        ForeignKeyConstraint(
            ["project_id", "rework_basis_ref"],
            [
                f"{SCHEMA_NAME}.rework_tasks.project_id",
                f"{SCHEMA_NAME}.rework_tasks.entity_id",
            ],
            name="fk_shots_rework_basis",
        ),
        CheckConstraint("shot_id = entity_id", name="ck_shots_identity_single"),
        CheckConstraint(
            "(status = 'BLOCKED' AND blocked_reason IS NOT NULL) "
            "OR (status = 'CANCELLED' AND cancellation_ref IS NOT NULL "
            "AND cancelled_at IS NOT NULL) "
            "OR (status NOT IN ('BLOCKED', 'CANCELLED') "
            "AND blocked_reason IS NULL AND cancellation_ref IS NULL "
            "AND cancelled_at IS NULL)",
            name="ck_shots_evidence_consistency",
        ),
        status_check((s.value for s in ShotStatus), table="shots"),
        {"schema": SCHEMA_NAME},
    )

    shot_id: Mapped[str] = mapped_column(String(64), nullable=False)
    shot_code: Mapped[str] = mapped_column(String(64), nullable=False)
    scene_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    shot_revision_id: Mapped[str] = mapped_column(String(64), nullable=False)
    blocked_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    rework_basis_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    cancellation_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    source_draft_ref: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True
    )


class ShotRevisionModel(EntityMixin, Base):
    """shot_revisions 表模型（镜头内容修订）。

    - (shot_id, revision_no) 唯一：修订号在同一镜头内单调且不重复。
    - CHECK (shot_revision_id = entity_id)：契约 SR-1「身份单一」。与 SC-1 / SH-1
      同批下沉，避免「同类不变量只修两条」的口径不一致。
    - 部分唯一索引：每个 Shot 恰有一个 ACTIVE 修订（ADR-R17）。
      并发确认时第二个请求撞唯一索引并抛 409——这正是需要的行为。
    - (project_id, entity_id) 唯一（A22）：**不是**冗余索引，而是
      ``shot_specs(project_id, shot_revision_id)`` 复合外键的引用目标。单列外键
      只能证明修订存在，无法阻止「规格属项目 A、修订属项目 B」（与 A13 在
      scenes/shots 上用的是同一手法）。
    """

    __tablename__ = "shot_revisions"
    __table_args__ = (
        UniqueConstraint(
            "shot_id", "revision_no", name="uq_shot_revision_no"
        ),
        UniqueConstraint(
            "project_id",
            "entity_id",
            name="uq_shot_revisions_entity_project",
        ),
        Index(
            "ux_shot_active_revision",
            "shot_id",
            unique=True,
            postgresql_where="status = 'ACTIVE'",
        ),
        ForeignKeyConstraint(
            ["project_id", "shot_id"],
            [f"{SCHEMA_NAME}.shots.project_id", f"{SCHEMA_NAME}.shots.entity_id"],
            name="fk_shot_revisions_shot",
        ),
        CheckConstraint(
            "shot_revision_id = entity_id", name="ck_shot_revisions_identity_single"
        ),
        status_check((s.value for s in ShotRevisionStatus), table="shot_revisions"),
        {"schema": SCHEMA_NAME},
    )

    shot_revision_id: Mapped[str] = mapped_column(String(64), nullable=False)
    shot_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    episode_id: Mapped[str] = mapped_column(String(64), nullable=False)
    scene_id: Mapped[str] = mapped_column(String(64), nullable=False)
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    narrative_intent: Mapped[str] = mapped_column(Text, nullable=False)
    character_refs: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    location_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    prop_refs: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    constraint_pack_applicability: Mapped[str] = mapped_column(
        String(64), nullable=False
    )
    constraint_pack_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    creative_autonomy: Mapped[str] = mapped_column(String(64), nullable=False)
    risk: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    quality_profile: Mapped[str] = mapped_column(String(64), nullable=False)
    intent_ref: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    basis_script_revision_id: Mapped[str] = mapped_column(String(64), nullable=False)
    script_anchors: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    source_draft_ref: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True
    )
    source_candidate_refs: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    change_origin: Mapped[str] = mapped_column(String(64), nullable=False)
    supersedes_shot_revision_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    change_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    changed_fields: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    provenance: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    confirmed_by: Mapped[str] = mapped_column(String(64), nullable=False)
    confirmed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class ShotSpecModel(EntityMixin, Base):
    """shot_specs 表模型（镜头结构化规格，绑定精确修订）。

    为什么这张表的约束几乎全是「结构性的」（每条都想清楚放在哪一层）：

    - ``uq_shot_spec_per_revision (shot_revision_id)``：契约 SHOTSPEC.CREATE 的
      审批要求明文「同一修订至多一份规格」。用部分索引或服务层查重都不行——
      并发两个请求都能「查到没有」再各自插入，唯一约束是唯一能真正拒绝的东西。
    - ``fk_shot_specs_revision (project_id, shot_revision_id)``：**复合**而非单列。
      单列外键只能证明修订存在，无法阻止「规格属项目 A、修订属项目 B」，负测
      ACT-SPEC-NEG-002 正好打这条。
    - ``ck_shot_specs_cut_points``：SS-3 下沉到库层。绕过服务直连 DB 的写入
      （运维脚本、后续批处理）也必须被拒。递增性用 IMMUTABLE 函数表达——
      CHECK 里不允许子查询，而「任意长度的数组严格递增」没有纯表达式写法。
      末点上限直接在 CHECK 里算，与领域层 ``total_frames()`` 同一算式（整数
      除法，正数截断），避免「库里合法、领域拒绝」的分歧。
    - ``ck_shot_specs_identity_single``：与 SC-1 / SH-1 / SR-1 同形。``shot_spec_id``
      在 schema 里是独立字段，但它一旦可以不等 ``entity_id``，库里就有两套 ID。

    ``cut_points_frames`` 用 ``integer[]`` 而不是 JSONB：元素类型、非空数组、
    「首点为 0」这些都可以由类型系统与 CHECK 直接表达，JSONB 只能靠应用层自觉。
    ``control_signal_refs`` 用 JSONB 是因为契约只给了 ``type: array``，没有元素
    结构——把它固定成 text[] 会让未来的结构化引用无处安放。
    """

    __tablename__ = "shot_specs"
    __table_args__ = (
        UniqueConstraint("shot_revision_id", name="uq_shot_spec_per_revision"),
        UniqueConstraint(
            "project_id",
            "entity_id",
            name="uq_shot_specs_entity_project",
        ),
        ForeignKeyConstraint(
            ["project_id", "shot_revision_id"],
            [
                f"{SCHEMA_NAME}.shot_revisions.project_id",
                f"{SCHEMA_NAME}.shot_revisions.entity_id",
            ],
            name="fk_shot_specs_revision",
        ),
        CheckConstraint(
            "shot_spec_id = entity_id", name="ck_shot_specs_identity_single"
        ),
        CheckConstraint("duration_ms >= 1", name="ck_shot_specs_duration_positive"),
        CheckConstraint(
            "fps_num >= 1 AND fps_den >= 1", name="ck_shot_specs_fps_positive"
        ),
        CheckConstraint(
            f"{SCHEMA_NAME}.cut_points_monotonic(cut_points_frames) "
            "AND cut_points_frames[array_length(cut_points_frames, 1)] "
            "< (duration_ms * fps_num) / (1000 * fps_den)",
            name="ck_shot_specs_cut_points",
        ),
        status_check((s.value for s in ShotSpecStatus), table="shot_specs"),
        {"schema": SCHEMA_NAME},
    )

    shot_spec_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    shot_revision_id: Mapped[str] = mapped_column(String(64), nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    fps_num: Mapped[int] = mapped_column(Integer, nullable=False)
    fps_den: Mapped[int] = mapped_column(Integer, nullable=False)
    cut_points_frames: Mapped[list[int]] = mapped_column(
        ARRAY(Integer), nullable=False
    )
    aspect_ratio: Mapped[str] = mapped_column(String(32), nullable=False)
    coordinate_system: Mapped[str] = mapped_column(String(32), nullable=False)
    camera_path: Mapped[str] = mapped_column(Text, nullable=False)
    subject_paths: Mapped[str] = mapped_column(Text, nullable=False)
    shot_size_changes: Mapped[str] = mapped_column(String(500), nullable=False)
    previz_ref: Mapped[str] = mapped_column(String(200), nullable=False)
    control_signal_refs: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    locked_dimensions: Mapped[str] = mapped_column(
        String(512), nullable=False, default=""
    )


class ConstraintPackModel(EntityMixin, Base):
    """constraint_packs 表模型（隐式连续性约束包，7.9 / A23）。

    每条约束都在回答「绕过服务的写入会不会破坏 7.9」，而不是「服务层已经查过」：

    - ``fk_constraint_packs_project_tenant (project_id, tenant_id)``：**租户复合**
      外键。单列 ``project_id`` 外键只能证明项目存在，挡不住「包属租户 A、项目
      属租户 B」。复合外键把「包与项目同租户」放到库里（与 projects 的
      ``uq_projects_entity_tenant`` 对齐）。RLS 是查询侧的隔离，外键是写入侧的
      归属——两者不能互相替代。
    - ``uq_constraint_pack_version (constraint_pack_id, version)``：7.9「包标识，
      同版本内唯一；变更须新建版本并 supersede 旧版本」。两个不同内容自称同一
      版本号，会让「哪一份是 v3」没有答案。
    - ``uq_constraint_pack_single_active``（部分唯一，``WHERE status='ACTIVE'``）：
      7.9「只有 ACTIVE 可作为生成输入」。同一逻辑包若允许两个 ACTIVE 版本并存，
      下游按 pack 查询会拿到两份互斥的锁定维度，而「用哪一份」只能靠调用方猜。
      这条索引同时把「版本推进必须 supersede 旧版本」变成结构性要求：没有
      SUPERSEDE 入口时，新版本锁不上——这是刻意的，缺口应当表现为可读的拒绝，
      而不是静默的双 ACTIVE。
    - ``ck_constraint_packs_dimensions_known``：7.9 用「：」列举了七个维度，
      那就是枚举。``<@`` 是包含于，允许空数组（NOT_APPLICABLE 的包没有维度），
      但任一非空元素都必须在词表内。
    - ``ck_constraint_packs_entries``：条目八字段必填下沉到库层。用 IMMUTABLE
      函数而不是表达式：CHECK 不能写子查询，而「遍历 JSONB 数组逐条判」没有
      等价的纯表达式写法。
    - ``ck_constraint_packs_not_applicable_empty``：CP-4 的自述一致性。
      ``applicability='NOT_APPLICABLE'`` 与「列了锁定维度/条目」不能同时成立。

    刻意**没有** ``ck_constraint_packs_identity_single``
    --------------------------------------------------
    Scene/Shot/ShotRevision/ShotSpec 都有这条 CHECK（SC-1 / SH-1 / SR-1「身份单一」），
    本表没有，且不应补上：那几个对象的契约要求业务 id 与 entity_id 同一，本对象相反——
    ``entity_id`` 标识**版本行**，``constraint_pack_id`` 标识**逻辑包**。7.9 的
    ``version`` 写「变更须新建版本并 supersede 旧版本」，新版本就是「新行 + 同包标识
    + version 递增」。加上那条 CHECK 会让 ``uq_constraint_pack_version`` 与
    ``uq_constraint_pack_single_active`` 变成结构上恒真的约束（同一行的两列不可能
    让同一行冲突两次），即两条不变量被登记成 RUNTIME_ENFORCED 却什么也没挡住。

    为什么 ``constraints`` 用 JSONB 而不是子表：7.9 把它定义为包内 ``object[]``，
    条目没有独立生命周期与状态机；建表会让「包内条目」有第二个可被直接引用的
    入口，而契约只承认前者。``shot_ids`` 用 ``text[]`` 是因为它确实是一个集合
    （元素唯一、非空），类型系统能直接表达，JSONB 只能靠应用层自觉。
    """

    __tablename__ = "constraint_packs"
    __table_args__ = (
        UniqueConstraint(
            "constraint_pack_id", "version", name="uq_constraint_pack_version"
        ),
        # 子表复合外键的引用目标（A13 同形）；也是包在项目内的身份唯一性。
        UniqueConstraint(
            "project_id", "entity_id", name="uq_constraint_packs_entity_project"
        ),
        ForeignKeyConstraint(
            ["project_id", "tenant_id"],
            [
                f"{SCHEMA_NAME}.projects.entity_id",
                f"{SCHEMA_NAME}.projects.tenant_id",
            ],
            name="fk_constraint_packs_project_tenant",
        ),
        Index(
            "uq_constraint_pack_single_active",
            "constraint_pack_id",
            unique=True,
            postgresql_where=text("status = 'ACTIVE'"),
        ),
        CheckConstraint(
            "applicability IN ('APPLICABLE', 'NOT_APPLICABLE')",
            name="ck_constraint_packs_applicability",
        ),
        CheckConstraint(
            "cardinality(shot_ids) >= 1", name="ck_constraint_packs_shot_ids_present"
        ),
        CheckConstraint(
            f"{SCHEMA_NAME}.text_array_unique(shot_ids::text[])",
            name="ck_constraint_packs_shot_ids_unique",
        ),
        CheckConstraint(
            f"{SCHEMA_NAME}.text_array_unique(shot_group_ids::text[])",
            name="ck_constraint_packs_shot_groups_unique",
        ),
        CheckConstraint(
            "locked_dimensions::text[] <@ ARRAY["
            + ", ".join(f"'{name}'" for name in sorted(LOCKED_DIMENSIONS))
            + "]::text[]",
            name="ck_constraint_packs_dimensions_known",
        ),
        CheckConstraint(
            f"{SCHEMA_NAME}.constraint_entries_complete(constraints)",
            name="ck_constraint_packs_entries",
        ),
        CheckConstraint(
            "applicability <> 'NOT_APPLICABLE' OR "
            "(cardinality(locked_dimensions) = 0 "
            "AND jsonb_array_length(constraints) = 0)",
            name="ck_constraint_packs_not_applicable_empty",
        ),
        CheckConstraint("version >= 1", name="ck_constraint_packs_version_positive"),
        status_check((s.value for s in ConstraintPackStatus), table="constraint_packs"),
        {"schema": SCHEMA_NAME},
    )

    constraint_pack_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    scene_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    shot_ids: Mapped[list[str]] = mapped_column(ARRAY(String(64)), nullable=False)
    shot_group_ids: Mapped[list[str]] = mapped_column(
        ARRAY(String(64)), nullable=False
    )
    applicability: Mapped[str] = mapped_column(String(32), nullable=False)
    locked_dimensions: Mapped[list[str]] = mapped_column(
        ARRAY(String(32)), nullable=False
    )
    constraints: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    source_previz_revision: Mapped[str] = mapped_column(
        String(200), nullable=False, default=""
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    # 授权人：7.9「不得为 Agent」。这一句由授权层保证（Agent/Worker 角色没有
    # CONSTRAINT.PACK.LOCK 权限），不在库里按字符串猜主体类型。
    approved_by: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class ShotBreakdownDraftModel(EntityMixin, Base):
    """shot_breakdown_drafts 表模型（AI 拆镜草案）。

    CONFIRMED / RETURNED / DISCARDED 均为版本终态；修改走新 draft_version，
    由 root_draft_ref 与 supersedes_draft_ref 串成版本链。历史版本不覆盖、不删除。
    """

    __tablename__ = "shot_breakdown_drafts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["project_id"],
            [f"{SCHEMA_NAME}.projects.entity_id"],
            name="fk_shot_draft_project",
        ),
        status_check((s.value for s in ShotDraftStatus), table="shot_breakdown_drafts"),
        {"schema": SCHEMA_NAME},
    )

    shot_breakdown_draft_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    episode_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    basis_script_revision_id: Mapped[str] = mapped_column(String(64), nullable=False)
    breakdown_scope: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    plan_name: Mapped[str] = mapped_column(String(200), nullable=False)
    generation_method: Mapped[str] = mapped_column(String(64), nullable=False)
    draft_version: Mapped[int] = mapped_column(Integer, nullable=False)
    root_draft_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    supersedes_draft_ref: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    change_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    ai_rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    open_questions: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    potential_issues: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    scene_breakdowns: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    review_records: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    confirmed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    confirmed_shot_revision_refs: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False
    )
    returned_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    returned_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    return_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    discarded_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class ReworkTaskModel(EntityMixin, Base):
    """rework_tasks 表模型（返工任务）。

    归属 project_planning 而非资产域，是为了让 Shot.rework_basis_ref 的守卫
    能在同 schema 内用外键锁死（ADR-R28 §3.3）。affected_asset_version_ids 是
    跨域值引用，只能不加外键——这是已登记债务，不是疏漏。
    """

    __tablename__ = "rework_tasks"
    __table_args__ = (
        # 复合外键的引用目标：shots(project_id, rework_basis_ref) 指向它，锁死
        # 「镜头与其返工授权必须同项目」（P1-03）。entity_id 是 envelope 主键，
        # 与 project_id 组合唯一恒成立，但 PG 要求显式唯一键才能被外键引用。
        UniqueConstraint(
            "project_id",
            "entity_id",
            name="uq_rework_tasks_entity_project",
        ),
        ForeignKeyConstraint(
            ["project_id"],
            [f"{SCHEMA_NAME}.projects.entity_id"],
            name="fk_rework_tasks_project",
        ),
        status_check((s.value for s in ReworkTaskStatus), table="rework_tasks"),
        {"schema": SCHEMA_NAME},
    )

    rework_task_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    issue_id: Mapped[str] = mapped_column(String(64), nullable=False)
    target_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    defect_category: Mapped[str] = mapped_column(String(64), nullable=False)
    severity: Mapped[str] = mapped_column(String(32), nullable=False)
    root_cause: Mapped[str] = mapped_column(Text, nullable=False)
    owner: Mapped[str] = mapped_column(String(64), nullable=False)
    return_node: Mapped[str] = mapped_column(String(64), nullable=False)
    affected_asset_version_ids: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False
    )
    downstream_task_ids: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    constraint_pack_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)


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
        # 复合外键的引用目标（A13）：scenes(project_id, episode_id) 指向它，
        # 使「场次所属项目必须与集数所属项目一致」在数据库层成立。
        UniqueConstraint(
            "project_id",
            "entity_id",
            name="uq_episodes_entity_project",
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
