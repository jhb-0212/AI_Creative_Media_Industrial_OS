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
from packages.shared.rls import tenant_scope_predicate
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
def _row_visibility(table: str) -> str:
    """双维度可见性谓词（租户 + 组织），由单一事实源生成而非手抄。

    组织维度不冗余存 ``organization_id``（归属经 project 派生，冗余列会在项目
    换组织时成为第二事实源），改用父项目存在性表达：子查询里 ``projects`` 自己的
    策略会生效，组织规则只写一次、经父表传递给子表。

    ``p.tenant_id = <本表>.tenant_id`` 这条相关条件不能省。丢了它，谓词就退化成
    「当前租户里存在任意一个可见项目」即真——他组织的子行会整片放行，而只查
    ``EXISTS`` 子串的判据照样报 PASS。
    """
    return (
        "USING ("
        f"{tenant_scope_predicate()} AND "
        "EXISTS (SELECT 1 FROM project_planning.projects p "
        f"WHERE p.entity_id = {_SCHEMA}.{table}.project_id "
        f"AND p.tenant_id = {_SCHEMA}.{table}.tenant_id)"
        ")"
    )


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


def _ensure_episodes_entity_unique() -> None:
    """确保 episodes 上存在 (project_id, entity_id) 唯一键，且语义等价。

    fail-closed：若同名约束存在但列集不同（不是这两个列的唯一键），拒绝升级——
    静默跳过会让后续复合外键引用一个语义错误的键，那是比中止迁移更坏的失败。
    列顺序不参与比较：唯一性对列顺序不敏感，PG 外键引用只要求列集唯一。
    """
    bind = op.get_bind()
    existing = bind.execute(
        sa.text(
            """
            SELECT a.attname
            FROM pg_constraint c
            JOIN unnest(c.conkey) AS k(attnum) ON true
            JOIN pg_attribute a
              ON a.attrelid = c.conrelid AND a.attnum = k.attnum
            WHERE c.conrelid = to_regclass(:rel)
              AND c.conname = :name
              AND c.contype = 'u'
            """
        ),
        {"rel": f"{_SCHEMA}.episodes", "name": "uq_episodes_entity_project"},
    ).scalars().all()
    wanted = {"project_id", "entity_id"}
    if existing and set(existing) != wanted:
        raise RuntimeError(
            "uq_episodes_entity_project 已存在但列集不等价："
            f"实际={sorted(existing)}，期望={sorted(wanted)}；拒绝在不兼容约束上建外键"
        )
    if existing:
        return  # 同名同列集，复用即可（列序无关）
    op.create_unique_constraint(
        "uq_episodes_entity_project",
        "episodes",
        ["project_id", "entity_id"],
        schema=_SCHEMA,
    )


def upgrade() -> None:
    # 复合外键的引用目标：episodes(project_id, entity_id) 唯一。
    #
    # 幂等创建：这个唯一键在归属治理链（pending/feat-ownership/i9d0e1f2a3b4）
    # 与 ORM EpisodeModel 里也已声明，二者列序为 (entity_id, project_id)。唯一性
    # 与列顺序无关，PostgreSQL 的外键引用只要求被引用列集上有唯一约束。历史上
    # 同一测试库可能已被治理链迁移建过同名约束——无条件 create 会以 42710
    # 中止整条正式链。这里用 catalog 实判后跳过，既能从干净库升级，也能从
    # 混合残留库升级；跳过的前提是约束已存在且列集等价（可机检）。
    _ensure_episodes_entity_unique()

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

    # 行级安全：表一诞生就必须带着策略，而不是「以后补」。
    #
    # 为什么这不是可选项：本迁移之前，6 张业务表的策略已由 e9f0a1b2c3d4 统一升级为
    # 「租户 + 组织」。新增的 5 张表若只建表不加策略，它们在库层是**完全敞开**的——
    # project_planning_role 能读所有租户、所有组织的场次/镜头/返工任务，组织边界
    # 退回「只能靠应用层 authorize 判定」。e9f0 的迁移说明记录过同一个后果：一旦某条
    # 访问路径绕过 authorize（新端点漏挂依赖、Worker 直查、脚本直连），数据库不会拒绝，
    # 也没有任何告警。策略缺一个维度，写十条也只覆盖一个维度。
    for _table in sorted(_NEW_TABLES):
        op.execute(f"ALTER TABLE {_SCHEMA}.{_table} ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation_{_table} "
            f"ON {_SCHEMA}.{_table} " + _row_visibility(_table)
        )
        # FORCE：策略对表所有者同样生效。当前所有者是超级用户（FORCE 对超级用户
        # 与 BYPASSRLS 无效，这是 PG 语义），它的价值在「将来所有权转给非 BYPASSRLS
        # 角色后策略不会静默失效」；真正的运行时防线是启动期的 verify_rls_enforcement。
        op.execute(f"ALTER TABLE {_SCHEMA}.{_table} FORCE ROW LEVEL SECURITY")

    # 授权：建表之后按矩阵收敛（不手写 GRANT）。
    role = domain_role(_SCHEMA)
    for statement in converge_statements(role, _SCHEMA, tables=_NEW_TABLES):
        op.execute(statement)


def downgrade() -> None:
    # 先撤策略再删表：删表会连带删掉策略，但显式撤销让回滚文本自己说清
    # 「这张表曾经带过什么安全属性」，而不是依赖 DDL 的副作用。
    for _table in sorted(_NEW_TABLES, reverse=True):
        op.execute(
            f"DROP POLICY IF EXISTS tenant_isolation_{_table} ON {_SCHEMA}.{_table}"
        )
        op.execute(f"ALTER TABLE {_SCHEMA}.{_table} DISABLE ROW LEVEL SECURITY")

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

    # 刻意不删除 uq_episodes_entity_project：升级时若该键已存在（治理链残留），
    # a13b0 是复用而非创建，降级没有所有权删除它；干净库上虽由本迁移创建，
    # 但 EpisodeModel 本就声明了同名唯一键，保留它是合法目标状态。删除会让
    # 「降级到 c1a2」反而丢失一个领域模型声明的约束，属反向破坏。
