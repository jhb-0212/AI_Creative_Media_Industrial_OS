"""Scene / Shot / ShotRevision / ShotBreakdownDraft / ReworkTask 建表（A13 / ADR-R40）

为什么一次建五张表
------------------------------------------------------------
因为它们是同一条事务链，不是五个独立需求：``Shot.required`` 含
``shot_revision_id`` → 需要 ShotRevision → 唯一入口是 ``SHOT.REVISION.CONFIRM``
→ 其输入是 ShotBreakdownDraft → 唯一入口是 ``SHOT.DRAFT.CREATE``。
任何一环缺席，Shot 的响应模型就会因为必填字段缺失而 500，等于交付一个
必然报错的端点。

归属完整性（不放应用层）
------------------------------------------------------------
- ``(project_id, episode_id) → episodes(project_id, entity_id)``：单列外键只能
  证明集数存在，无法阻止「场次属项目 A、集数属项目 B」。为此本迁移先给
  episodes 补 ``uq_episodes_entity_project`` 作为复合外键的引用目标。
- ``(project_id, scene_id) → scenes(project_id, entity_id)``：同理锁死 Shot 与 Scene。
- ``(project_id, shot_id) → shots(project_id, entity_id)``：锁死修订与 Shot。
- ``rework_basis_ref → rework_tasks(entity_id)``：让 Shot 的返工守卫在库层可验证，
  而不是只校验字符串非空（ADR-R40 §3.3）。

守卫字段（四个列不是可选项）
------------------------------------------------------------
Shot 的 10 条边里 6 条带守卫，依赖 ``blocked_reason`` / ``rework_basis_ref`` /
``cancellation_ref`` / ``cancelled_at``。库层再加一条证据自洽 CHECK，与
``object_schemas`` 里 Shot 的 statement 逐字对应——领域层负责「错误可读」，
库层负责「绕过服务也写不进去」。

编号唯一性与身份单一（与冻结契约逐字对齐）
------------------------------------------------------------
- ``uq_scene_code_per_project (project_id, scene_code)``、``uq_shot_code_per_project
  (project_id, shot_code)``：契约 SC-3 / SH-3 要求「编号在**项目内**唯一」。早先
  实现成 (project_id, episode_id, scene_code) / (project_id, scene_id, shot_code)
  是弱于契约的口径——跨集、跨场可以重名，而契约不允许。既然契约已冻结，实现
  必须服从；编号在业务上按集编排，但唯一性必须按项目裁决，否则「项目内按
  scene_code 引用唯一场次」会有多个解。
- ``ck_scenes_identity_single`` / ``ck_shots_identity_single`` /
  ``ck_shot_revisions_identity_single``：契约 SC-1 / SH-1 / SR-1「身份单一」。
  领域层已强校验，但那只挡住经过 Service 的写入；CHECK 让绕过服务直连 DB 的
  写入（运维脚本、后续批处理）也失败。

权限
------------------------------------------------------------
不在本文件手写 GRANT：建表后调用 ``converge_statements(..., tables={新表})``，
权限仍由 ``packages/shared/db_grants.py`` 的矩阵生成。五张表都是
``SELECT INSERT UPDATE``，**无 DELETE**（删除语义是 ARCHIVED / CANCELLED /
SUPERSEDED）。

索引命名必须走 ``op.f()``
------------------------------------------------------------
SQLAlchemy 的默认索引名含 schema 前缀（``ix_project_planning_scenes_project_id``），
而本文件初版写成裸名 ``ix_scenes_project_id``。静态闸门全绿——因为
``alembic check`` 需要真实库才能比对；CI 一跑就报「26 条新建索引操作」。
所以本文件所有 ``index=True`` 对应的索引一律用 ``op.f("ix_project_planning_<table>_<col>")``，
名字取自已映射元数据（不是手抄），与模型侧逐字一致。

Revision ID: a13b0c1d2e3f
Revises: c1a2b3d4e5f6
Create Date: 2026-11-20
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from packages.shared.db_grants import PROJECT_PLANNING, converge_statements
from packages.shared.db_roles import domain_role
from packages.shared.status_constraint import status_check
from services.project_planning.domain.enums import (
    ReworkTaskStatus,
    SceneStatus,
    ShotDraftStatus,
    ShotRevisionStatus,
    ShotStatus,
)

revision: str = "a13b0c1d2e3f"
down_revision: str | Sequence[str] | None = "c1a2b3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCHEMA = PROJECT_PLANNING

# 本迁移建的表（授权范围，不是矩阵镜像）。
_NEW_TABLES: frozenset[str] = frozenset(
    {
        "scenes",
        "shots",
        "shot_revisions",
        "shot_breakdown_drafts",
        "rework_tasks",
    }
)

# C.3.1 信封列（与 packages/shared/orm.py::EntityMixin 逐列一致）。
# 不抽公共函数到迁移之外：迁移是时间胶囊，必须能在未来代码演进而它不变的情况下
# 仍然准确描述「这一刻的表长什么样」。
def _envelope_columns() -> list[sa.schema.Column]:
    return [
        sa.Column("entity_id", sa.String(64), primary_key=True),
        sa.Column("entity_type", sa.String(64), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("entity_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("owner_module", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.String(64), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("audit_ref", sa.String(128), nullable=True),
    ]


def upgrade() -> None:
    # 复合外键的引用目标：episodes(project_id, entity_id) 唯一。
    op.create_unique_constraint(
        "uq_episodes_entity_project",
        "episodes",
        ["project_id", "entity_id"],
        schema=_SCHEMA,
    )

    # --- rework_tasks（先建：shots 的 rework_basis_ref 引用它） ---
    op.create_table(
        "rework_tasks",
        *_envelope_columns(),
        sa.Column("rework_task_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("issue_id", sa.String(64), nullable=False),
        sa.Column("target_ref", sa.String(64), nullable=False),
        sa.Column("defect_category", sa.String(64), nullable=False),
        sa.Column("severity", sa.String(32), nullable=False),
        sa.Column("root_cause", sa.Text(), nullable=False),
        sa.Column("owner", sa.String(64), nullable=False),
        sa.Column("return_node", sa.String(64), nullable=False),
        sa.Column("affected_asset_version_ids", postgresql.JSONB(), nullable=False),
        sa.Column("downstream_task_ids", postgresql.JSONB(), nullable=False),
        sa.Column("constraint_pack_ref", sa.String(64), nullable=True),
        sa.ForeignKeyConstraint(
            ["project_id"],
            [f"{_SCHEMA}.projects.entity_id"],
            name="fk_rework_tasks_project",
        ),
        status_check((s.value for s in ReworkTaskStatus), table="rework_tasks"),
        schema=_SCHEMA,
    )
    op.create_index(
        op.f("ix_project_planning_rework_tasks_tenant_id"), "rework_tasks", ["tenant_id"], schema=_SCHEMA
    )
    op.create_index(
        op.f("ix_project_planning_rework_tasks_project_id"), "rework_tasks", ["project_id"], schema=_SCHEMA
    )

    # --- scenes ---
    op.create_table(
        "scenes",
        *_envelope_columns(),
        sa.Column("scene_id", sa.String(64), nullable=False),
        sa.Column("scene_code", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("episode_id", sa.String(64), nullable=False),
        sa.UniqueConstraint(
            "project_id", "entity_id", name="uq_scenes_entity_project"
        ),
        sa.UniqueConstraint(
            "project_id", "scene_code", name="uq_scene_code_per_project"
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "episode_id"],
            [
                f"{_SCHEMA}.episodes.project_id",
                f"{_SCHEMA}.episodes.entity_id",
            ],
            name="fk_scenes_episode",
        ),
        sa.CheckConstraint("scene_id = entity_id", name="ck_scenes_identity_single"),
        status_check((s.value for s in SceneStatus), table="scenes"),
        schema=_SCHEMA,
    )
    op.create_index(op.f("ix_project_planning_scenes_tenant_id"), "scenes", ["tenant_id"], schema=_SCHEMA)
    op.create_index(op.f("ix_project_planning_scenes_project_id"), "scenes", ["project_id"], schema=_SCHEMA)
    op.create_index(op.f("ix_project_planning_scenes_episode_id"), "scenes", ["episode_id"], schema=_SCHEMA)

    # --- shots ---
    op.create_table(
        "shots",
        *_envelope_columns(),
        sa.Column("shot_id", sa.String(64), nullable=False),
        sa.Column("shot_code", sa.String(64), nullable=False),
        sa.Column("scene_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("shot_revision_id", sa.String(64), nullable=False),
        sa.Column("blocked_reason", sa.Text(), nullable=True),
        sa.Column("rework_basis_ref", sa.String(64), nullable=True),
        sa.Column("cancellation_ref", sa.String(128), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_draft_ref", postgresql.JSONB(), nullable=True),
        sa.UniqueConstraint("project_id", "entity_id", name="uq_shots_entity_project"),
        sa.UniqueConstraint(
            "project_id", "shot_code", name="uq_shot_code_per_project"
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "scene_id"],
            [f"{_SCHEMA}.scenes.project_id", f"{_SCHEMA}.scenes.entity_id"],
            name="fk_shots_scene",
        ),
        sa.ForeignKeyConstraint(
            ["rework_basis_ref"],
            [f"{_SCHEMA}.rework_tasks.entity_id"],
            name="fk_shots_rework_basis",
        ),
        sa.CheckConstraint("shot_id = entity_id", name="ck_shots_identity_single"),
        sa.CheckConstraint(
            "(status = 'BLOCKED' AND blocked_reason IS NOT NULL) "
            "OR (status = 'CANCELLED' AND cancellation_ref IS NOT NULL "
            "AND cancelled_at IS NOT NULL) "
            "OR (status NOT IN ('BLOCKED', 'CANCELLED') "
            "AND blocked_reason IS NULL AND cancellation_ref IS NULL "
            "AND cancelled_at IS NULL)",
            name="ck_shots_evidence_consistency",
        ),
        status_check((s.value for s in ShotStatus), table="shots"),
        schema=_SCHEMA,
    )
    op.create_index(op.f("ix_project_planning_shots_tenant_id"), "shots", ["tenant_id"], schema=_SCHEMA)
    op.create_index(op.f("ix_project_planning_shots_scene_id"), "shots", ["scene_id"], schema=_SCHEMA)
    op.create_index(op.f("ix_project_planning_shots_project_id"), "shots", ["project_id"], schema=_SCHEMA)

    # --- shot_revisions ---
    op.create_table(
        "shot_revisions",
        *_envelope_columns(),
        sa.Column("shot_revision_id", sa.String(64), nullable=False),
        sa.Column("shot_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("episode_id", sa.String(64), nullable=False),
        sa.Column("scene_id", sa.String(64), nullable=False),
        sa.Column("revision_no", sa.Integer(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("narrative_intent", sa.Text(), nullable=False),
        sa.Column("character_refs", postgresql.JSONB(), nullable=False),
        sa.Column("location_ref", sa.String(64), nullable=False),
        sa.Column("prop_refs", postgresql.JSONB(), nullable=False),
        sa.Column("constraint_pack_applicability", sa.String(64), nullable=False),
        sa.Column("constraint_pack_id", sa.String(64), nullable=True),
        sa.Column("creative_autonomy", sa.String(64), nullable=False),
        sa.Column("risk", postgresql.JSONB(), nullable=False),
        sa.Column("quality_profile", sa.String(64), nullable=False),
        sa.Column("intent_ref", postgresql.JSONB(), nullable=False),
        sa.Column("basis_script_revision_id", sa.String(64), nullable=False),
        sa.Column("script_anchors", postgresql.JSONB(), nullable=False),
        sa.Column("source_draft_ref", postgresql.JSONB(), nullable=True),
        sa.Column("source_candidate_refs", postgresql.JSONB(), nullable=False),
        sa.Column("change_origin", sa.String(64), nullable=False),
        sa.Column("supersedes_shot_revision_id", sa.String(64), nullable=True),
        sa.Column("change_summary", sa.Text(), nullable=True),
        sa.Column("changed_fields", postgresql.JSONB(), nullable=False),
        sa.Column("provenance", postgresql.JSONB(), nullable=False),
        sa.Column("confirmed_by", sa.String(64), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("shot_id", "revision_no", name="uq_shot_revision_no"),
        sa.ForeignKeyConstraint(
            ["project_id", "shot_id"],
            [f"{_SCHEMA}.shots.project_id", f"{_SCHEMA}.shots.entity_id"],
            name="fk_shot_revisions_shot",
        ),
        sa.CheckConstraint(
            "shot_revision_id = entity_id", name="ck_shot_revisions_identity_single"
        ),
        status_check((s.value for s in ShotRevisionStatus), table="shot_revisions"),
        schema=_SCHEMA,
    )
    op.create_index(
        op.f("ix_project_planning_shot_revisions_tenant_id"), "shot_revisions", ["tenant_id"], schema=_SCHEMA
    )
    op.create_index(
        op.f("ix_project_planning_shot_revisions_shot_id"), "shot_revisions", ["shot_id"], schema=_SCHEMA
    )
    op.create_index(
        op.f("ix_project_planning_shot_revisions_project_id"),
        "shot_revisions",
        ["project_id"],
        schema=_SCHEMA,
    )
    # 每个 Shot 恰一个 ACTIVE 修订（ADR-R17）；并发确认由唯一索引兜底。
    op.create_index(
        "ux_shot_active_revision",
        "shot_revisions",
        ["shot_id"],
        unique=True,
        schema=_SCHEMA,
        postgresql_where=sa.text("status = 'ACTIVE'"),
    )

    # --- shot_breakdown_drafts ---
    op.create_table(
        "shot_breakdown_drafts",
        *_envelope_columns(),
        sa.Column("shot_breakdown_draft_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("episode_ref", sa.String(64), nullable=True),
        sa.Column("basis_script_revision_id", sa.String(64), nullable=False),
        sa.Column("breakdown_scope", postgresql.JSONB(), nullable=False),
        sa.Column("plan_name", sa.String(200), nullable=False),
        sa.Column("generation_method", sa.String(64), nullable=False),
        sa.Column("draft_version", sa.Integer(), nullable=False),
        sa.Column("root_draft_ref", sa.String(64), nullable=False),
        sa.Column("supersedes_draft_ref", sa.String(64), nullable=True),
        sa.Column("change_summary", sa.Text(), nullable=True),
        sa.Column("ai_rationale", sa.Text(), nullable=True),
        sa.Column("open_questions", postgresql.JSONB(), nullable=False),
        sa.Column("potential_issues", postgresql.JSONB(), nullable=False),
        sa.Column("scene_breakdowns", postgresql.JSONB(), nullable=False),
        sa.Column("review_records", postgresql.JSONB(), nullable=False),
        sa.Column("confirmed_by", sa.String(64), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("confirmed_shot_revision_refs", postgresql.JSONB(), nullable=False),
        sa.Column("returned_by", sa.String(64), nullable=True),
        sa.Column("returned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("return_reason", sa.Text(), nullable=True),
        sa.Column("discarded_reason", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["project_id"],
            [f"{_SCHEMA}.projects.entity_id"],
            name="fk_shot_draft_project",
        ),
        status_check((s.value for s in ShotDraftStatus), table="shot_breakdown_drafts"),
        schema=_SCHEMA,
    )
    op.create_index(
        op.f("ix_project_planning_shot_breakdown_drafts_tenant_id"),
        "shot_breakdown_drafts",
        ["tenant_id"],
        schema=_SCHEMA,
    )
    op.create_index(
        op.f("ix_project_planning_shot_breakdown_drafts_project_id"),
        "shot_breakdown_drafts",
        ["project_id"],
        schema=_SCHEMA,
    )

    # 授权：建表之后按矩阵收敛（不手写 GRANT）。
    role = domain_role(_SCHEMA)
    for statement in converge_statements(role, _SCHEMA, tables=_NEW_TABLES):
        op.execute(statement)


def downgrade() -> None:
    op.drop_index(
        op.f("ix_project_planning_shot_breakdown_drafts_project_id"),
        table_name="shot_breakdown_drafts",
        schema=_SCHEMA,
    )
    op.drop_index(
        op.f("ix_project_planning_shot_breakdown_drafts_tenant_id"),
        table_name="shot_breakdown_drafts",
        schema=_SCHEMA,
    )
    op.drop_table("shot_breakdown_drafts", schema=_SCHEMA)

    op.drop_index(
        "ux_shot_active_revision", table_name="shot_revisions", schema=_SCHEMA
    )
    op.drop_index(
        op.f("ix_project_planning_shot_revisions_project_id"), table_name="shot_revisions", schema=_SCHEMA
    )
    op.drop_index(
        op.f("ix_project_planning_shot_revisions_shot_id"), table_name="shot_revisions", schema=_SCHEMA
    )
    op.drop_index(
        op.f("ix_project_planning_shot_revisions_tenant_id"), table_name="shot_revisions", schema=_SCHEMA
    )
    op.drop_table("shot_revisions", schema=_SCHEMA)

    op.drop_index(op.f("ix_project_planning_shots_project_id"), table_name="shots", schema=_SCHEMA)
    op.drop_index(op.f("ix_project_planning_shots_scene_id"), table_name="shots", schema=_SCHEMA)
    op.drop_index(op.f("ix_project_planning_shots_tenant_id"), table_name="shots", schema=_SCHEMA)
    op.drop_table("shots", schema=_SCHEMA)

    op.drop_index(op.f("ix_project_planning_scenes_episode_id"), table_name="scenes", schema=_SCHEMA)
    op.drop_index(op.f("ix_project_planning_scenes_project_id"), table_name="scenes", schema=_SCHEMA)
    op.drop_index(op.f("ix_project_planning_scenes_tenant_id"), table_name="scenes", schema=_SCHEMA)
    op.drop_table("scenes", schema=_SCHEMA)

    op.drop_index(
        op.f("ix_project_planning_rework_tasks_project_id"), table_name="rework_tasks", schema=_SCHEMA
    )
    op.drop_index(
        op.f("ix_project_planning_rework_tasks_tenant_id"), table_name="rework_tasks", schema=_SCHEMA
    )
    op.drop_table("rework_tasks", schema=_SCHEMA)

    op.drop_constraint(
        "uq_episodes_entity_project", "episodes", schema=_SCHEMA, type_="unique"
    )
