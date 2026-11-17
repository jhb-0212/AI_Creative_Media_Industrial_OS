"""镜头规划域：shot_specs / shot_visual_plans / constraint_packs（RLS + 状态机守卫）。

Revision ID: d5e6f7a8b9c0
Revises: b1a2c3d4e5f6
Create Date: 2026-11-17

背景（ADR-R27 阶段 1C）：
在独立 ``shot_planning`` schema 内落地镜头域后三个对象：
- ShotSpec（8.12 镜头结构化规格单一数据源，绑定 ShotRevision）；
- ShotVisualPlan（19.2 视觉方案，锁定/开放字段分离）；
- ConstraintPack（7.9 隐式连续性依赖约束包）。

结构约束（对齐 b1a2c3d4e5f6 与资产域 b3c4d5e6f7a8 范式）：
1. 三张表全部落在独立 schema ``shot_planning``，复用 ``svc_shot_planning``
   角色（ADR-R16：数据所有权按域拆分，域内新增表不需要新角色）；
2. RLS：租户隔离 + 项目范围（RESTRICTIVE）双策略，与既有镜头表同口径；
3. 状态机下沉数据库层（ADR-R20）：
   - ConstraintPack（7.9 权威状态机）：转移表 + 触发器守卫，
     未登记边在数据库层被拒——服务层判定在 domain/state_machine.py，
     直连 SQL 的写入不经服务层，必须在这里拦；
   - ShotVisualPlan（C.7）：同上；
   - ShotSpec：无生命周期状态机（31.5 未登记），但规格内容列不可改写
     （C2 历史版本不可改写），以不可变内容列守卫落地；
4. 授权口径：三张表 INSERT + SELECT + UPDATE（状态推进/锁定是唯一
   变更路径），不授 DELETE（业务对象不可物理删除）。

转移字面量刻意写成模块常量而不是从代码导入：迁移是历史快照，若它随代码
变化，重放同一个 revision 会得到不同的库。一致性由
``tests/unit/test_enum_parity.py`` 比对这份字面量与当前枚举/转移表来守。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from packages.shared.db_roles import DatabaseDomain, role_for

# revision identifiers, used by Alembic.
revision: str = "d5e6f7a8b9c0"
down_revision: str | Sequence[str] | None = "b1a2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "shot_planning"

#: 契约声明的状态取值（迁移是历史快照，不 import 代码）。
SHOT_VISUAL_PLAN_STATUS_VALUES = (
    "APPROVED",
    "DRAFT",
    "LOCKED",
    "PARTIALLY_LOCKED",
    "REJECTED",
    "REVIEW",
)
CONSTRAINT_PACK_STATUS_VALUES = (
    "ACTIVE",
    "DRAFT",
    "REVOKED",
    "STALE",
    "SUPERSEDED",
)

#: 播种用的状态转移边 —— 必须与领域转移表完全一致（7.9 / C.7）。
SHOT_VISUAL_PLAN_STATUS_TRANSITIONS: tuple[tuple[str, str], ...] = (
    ("DRAFT", "REVIEW"),
    ("REVIEW", "APPROVED"),
    ("REVIEW", "REJECTED"),
    ("APPROVED", "PARTIALLY_LOCKED"),
    ("APPROVED", "LOCKED"),
    ("PARTIALLY_LOCKED", "LOCKED"),
)
CONSTRAINT_PACK_STATUS_TRANSITIONS: tuple[tuple[str, str], ...] = (
    ("DRAFT", "ACTIVE"),
    ("ACTIVE", "STALE"),
    ("ACTIVE", "SUPERSEDED"),
    ("ACTIVE", "REVOKED"),
)

#: 各表的状态/锁定路径合法可变列（守卫白名单）。
#: 白名单之外的列一律视为不可变内容列（C2 历史版本不可改写）：
#: - 公共可变：status（过转移表判定）/ entity_version / updated_at / audit_ref；
#: - shot_specs：锁定元数据（锁定人/时间/理由，19.2）；
#: - shot_visual_plans：锁定/开放字段集合（逐字段授权，19.2）；
#: - constraint_packs：审批人/审批时间（7.9 激活必填）。
_MUTABLE_COLUMNS: dict[str, tuple[str, ...]] = {
    "shot_specs": ("locked_dimensions", "locked_by", "locked_at", "lock_rationale"),
    "shot_visual_plans": ("locked_fields", "open_fields"),
    "constraint_packs": ("approved_by", "approved_at"),
}


def _scope_expression(column: str) -> str:
    """项目归属判定表达式（与 b1a2c3d4e5f6 的策略文本保持同一口径）。"""
    return f"""(
        organization_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid
        AND (
            current_setting('app.current_projects', true) = '*'
            OR {column} = ANY (
                string_to_array(
                    NULLIF(current_setting('app.current_projects', true), ''), ','
                )
            )
        )
    )"""


def _enable_rls(table: str, project_column: str) -> None:
    """给表加租户隔离 + 项目范围（限制性）策略，与既有镜头级资源表同口径。"""
    op.execute(f"ALTER TABLE {SCHEMA}.{table} ENABLE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY tenant_isolation_{table}
        ON {SCHEMA}.{table}
        USING (
            tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid
        )
        """
    )
    expression = _scope_expression(project_column)
    op.execute(
        f"""
        CREATE POLICY project_scope_{table}
        ON {SCHEMA}.{table}
        AS RESTRICTIVE
        FOR ALL
        USING ({expression})
        WITH CHECK ({expression})
        """
    )


def _create_transitions_table(table: str) -> None:
    """建转移表（空壳，由调用方播种）。"""
    op.create_table(
        table,
        sa.Column("from_status", sa.String(32), primary_key=True, nullable=False),
        sa.Column("to_status", sa.String(32), primary_key=True, nullable=False),
        schema=SCHEMA,
    )


def _seed_transitions(table: str, edges: tuple[tuple[str, str], ...]) -> None:
    """播种已登记边。"""
    transitions = sa.table(
        table,
        sa.column("from_status", sa.String),
        sa.column("to_status", sa.String),
        schema=SCHEMA,
    )
    op.bulk_insert(
        transitions,
        [{"from_status": src, "to_status": tgt} for src, tgt in edges],
    )


def _status_check(table: str, constraint: str, values: tuple[str, ...]) -> None:
    """status 只能取契约登记的状态值。"""
    joined = ", ".join(f"'{v}'" for v in values)
    op.execute(
        f"ALTER TABLE {SCHEMA}.{table} "
        f"ADD CONSTRAINT {constraint} CHECK (status IN ({joined}))"
    )


def _create_guard(table: str, transitions_table: str | None) -> None:
    """建状态转移 + 不可变内容列守卫触发器（ADR-R20 范式）。

    - ``transitions_table`` 非空：status 变化必须沿转移表登记过的边推进；
    - 不可变内容列：白名单（_MUTABLE_COLUMNS）之外的列值发生变化即拒绝，
      逐列扫描真实列，新增内容列自动纳入保护，不依赖手写黑名单；
    - DELETE 一律拒绝（业务对象不可物理删除）；
    - 触发器只挂 UPDATE/DELETE（对齐 d1e2f3a4b5c6 范式）：INSERT 时 OLD 为空
      行，status 合法性由 CHECK 约束与初始状态构造校验保证，不参与转移判定。
    """
    transition_check = ""
    if transitions_table is not None:
        transition_check = f"""
    IF old_status IS DISTINCT FROM new_status THEN
        IF NOT EXISTS (
            SELECT 1 FROM {SCHEMA}.{transitions_table} t
            WHERE t.from_status = old_status AND t.to_status = new_status
        ) THEN
            RAISE EXCEPTION
                '非法状态转移 % -> %（未在 {transitions_table} 登记，ADR-R20）',
                old_status, new_status;
        END IF;
    END IF;
"""
    mutable = ", ".join(repr(c) for c in _MUTABLE_COLUMNS[table])
    op.execute(
        f"""
        CREATE FUNCTION {SCHEMA}.{table}_guard()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            col text;
            old_status text;
            new_status text;
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION
                    '{SCHEMA}.{table} 业务对象不可物理删除（ADR-R27）';
            END IF;

            old_status := to_jsonb(OLD) ->> 'status';
            new_status := to_jsonb(NEW) ->> 'status';
{transition_check}
            FOR col IN
                SELECT attname
                FROM pg_attribute
                WHERE attrelid = '{SCHEMA}.{table}'::regclass
                  AND attnum > 0
                  AND NOT attisdropped
            LOOP
                IF col IN ('status', 'entity_version', 'updated_at', 'audit_ref') THEN
                    CONTINUE;
                END IF;
                IF col IN ({mutable}) THEN
                    CONTINUE;
                END IF;
                IF (to_jsonb(OLD) ->> col) IS DISTINCT FROM (to_jsonb(NEW) ->> col) THEN
                    RAISE EXCEPTION
                        '{SCHEMA}.{table}.% 为不可变内容列，禁止改写（ADR-R27/C2）', col;
                END IF;
            END LOOP;

            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        f"CREATE TRIGGER {table}_guard_trigger "
        f"BEFORE UPDATE OR DELETE ON {SCHEMA}.{table} "
        f"FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.{table}_guard()"
    )


def _envelope_columns() -> list[sa.Column]:
    """C.3.1 通用实体信封列（与 b1a2c3d4e5f6 完全同构）。"""
    return [
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
    ]


def upgrade() -> None:
    role = role_for(DatabaseDomain.SHOT_PLANNING)

    # ---- shot_specs：镜头结构化规格（8.12） --------------------------
    op.create_table(
        "shot_specs",
        *_envelope_columns(),
        # ShotSpec 业务列（契约字段）
        sa.Column("shot_revision_id", sa.String(64), nullable=False),
        sa.Column("shot_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("aspect_ratio", sa.String(32), nullable=False),
        sa.Column("fps", sa.String(32), nullable=False),
        sa.Column("coordinate_system", sa.Text(), nullable=False),
        sa.Column("camera_path", postgresql.JSONB(), nullable=False),
        sa.Column("subject_paths", postgresql.JSONB(), nullable=False),
        sa.Column("shot_size_changes", postgresql.JSONB(), nullable=False),
        sa.Column("cut_points_frames", postgresql.JSONB(), nullable=False),
        sa.Column("previz_ref", sa.String(128), nullable=True),
        sa.Column("control_signal_refs", postgresql.JSONB(), nullable=False),
        sa.Column("locked_dimensions", postgresql.JSONB(), nullable=False),
        # 锁定元数据（19.2：锁定人/时间/理由；审计引用走信封 audit_ref 列）
        sa.Column("locked_by", sa.String(64), nullable=True),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lock_rationale", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status IN ('ACTIVE')",
            name="ck_shot_planning_shot_specs_status_registered",
        ),
        schema=SCHEMA,
    )

    # ---- shot_visual_plans：镜头视觉方案（19.2 / C.7） ---------------
    op.create_table(
        "shot_visual_plans",
        *_envelope_columns(),
        # ShotVisualPlan 业务列（契约字段）
        sa.Column("shot_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("intent_summary", sa.Text(), nullable=False),
        sa.Column("camera_language", sa.String(128), nullable=False),
        sa.Column("shot_size", sa.String(32), nullable=False),
        sa.Column("lens_range", sa.String(64), nullable=False),
        sa.Column("motion_intent", sa.Text(), nullable=False),
        sa.Column("lighting_intent", sa.Text(), nullable=False),
        sa.Column("locked_fields", postgresql.JSONB(), nullable=False),
        sa.Column("open_fields", postgresql.JSONB(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        schema=SCHEMA,
    )

    # ---- constraint_packs：隐式连续性依赖约束包（7.9） ---------------
    op.create_table(
        "constraint_packs",
        *_envelope_columns(),
        # ConstraintPack 业务列（契约字段）
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("scene_id", sa.String(64), nullable=True),
        sa.Column("shot_ids", postgresql.JSONB(), nullable=False),
        sa.Column("shot_group_ids", postgresql.JSONB(), nullable=False),
        sa.Column("applicability", sa.String(16), nullable=False),
        sa.Column("locked_dimensions", postgresql.JSONB(), nullable=False),
        sa.Column("constraints", postgresql.JSONB(), nullable=False),
        sa.Column("source_previz_revision", sa.String(64), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("approved_by", sa.String(64), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "applicability IN ('APPLICABLE', 'NOT_APPLICABLE')",
            name="ck_shot_planning_constraint_packs_applicability_registered",
        ),
        schema=SCHEMA,
    )

    # 状态 CHECK + 转移表 + 守卫（状态机在数据库层成立，ADR-R20）。
    _status_check(
        "shot_visual_plans",
        "ck_shot_planning_shot_visual_plans_status_registered",
        SHOT_VISUAL_PLAN_STATUS_VALUES,
    )
    _status_check(
        "constraint_packs",
        "ck_shot_planning_constraint_packs_status_registered",
        CONSTRAINT_PACK_STATUS_VALUES,
    )
    _create_transitions_table("shot_visual_plan_status_transitions")
    _seed_transitions(
        "shot_visual_plan_status_transitions", SHOT_VISUAL_PLAN_STATUS_TRANSITIONS
    )
    _create_transitions_table("constraint_pack_status_transitions")
    _seed_transitions(
        "constraint_pack_status_transitions", CONSTRAINT_PACK_STATUS_TRANSITIONS
    )
    _create_guard("shot_specs", None)
    _create_guard("shot_visual_plans", "shot_visual_plan_status_transitions")
    _create_guard("constraint_packs", "constraint_pack_status_transitions")

    # 索引：tenant_id / organization_id（EntityMixin index=True 命名一致）+ 业务键。
    for table in ("shot_specs", "shot_visual_plans", "constraint_packs"):
        op.create_index(
            f"ix_shot_planning_{table}_tenant_id", table, ["tenant_id"], schema=SCHEMA
        )
        op.create_index(
            f"ix_shot_planning_{table}_organization_id",
            table,
            ["organization_id"],
            schema=SCHEMA,
        )
        op.create_index(
            f"ix_shot_planning_{table}_project_id", table, ["project_id"], schema=SCHEMA
        )
    op.create_index(
        "ix_shot_planning_shot_specs_shot_revision_id",
        "shot_specs",
        ["shot_revision_id"],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_shot_planning_shot_specs_shot_id", "shot_specs", ["shot_id"], schema=SCHEMA
    )
    op.create_index(
        "ix_shot_planning_shot_visual_plans_shot_id",
        "shot_visual_plans",
        ["shot_id"],
        schema=SCHEMA,
    )

    # RLS：租户隔离 + 项目范围（限制性）。
    _enable_rls("shot_specs", "project_id")
    _enable_rls("shot_visual_plans", "project_id")
    _enable_rls("constraint_packs", "project_id")

    # 授权：本域角色可读、可插入、可状态推进（UPDATE）；不授 DELETE。
    # 转移表只读（触发器函数 SECURITY INVOKER 需要能读参考表，否则会把
    # 「非法转移」和「没权限读参考表」混为同一个 permission denied）。
    for table in ("shot_specs", "shot_visual_plans", "constraint_packs"):
        op.execute(f"GRANT SELECT, INSERT, UPDATE ON {SCHEMA}.{table} TO {role}")
    for table in ("shot_visual_plan_status_transitions", "constraint_pack_status_transitions"):
        op.execute(f"GRANT SELECT ON {SCHEMA}.{table} TO {role}")


def downgrade() -> None:
    role = role_for(DatabaseDomain.SHOT_PLANNING)
    for table in ("shot_specs", "shot_visual_plans", "constraint_packs"):
        op.execute(f"REVOKE ALL ON {SCHEMA}.{table} FROM {role}")
    for table in ("shot_visual_plan_status_transitions", "constraint_pack_status_transitions"):
        op.execute(f"REVOKE ALL ON {SCHEMA}.{table} FROM {role}")
    for table in ("shot_specs", "shot_visual_plans", "constraint_packs"):
        op.execute(f"DROP TRIGGER IF EXISTS {table}_guard_trigger ON {SCHEMA}.{table}")
        op.execute(f"DROP FUNCTION IF EXISTS {SCHEMA}.{table}_guard()")
    for table in ("shot_visual_plan_status_transitions", "constraint_pack_status_transitions"):
        op.drop_table(table, schema=SCHEMA)
    for table in ("shot_specs", "shot_visual_plans", "constraint_packs"):
        for policy in (f"project_scope_{table}", f"tenant_isolation_{table}"):
            op.execute(f"DROP POLICY IF EXISTS {policy} ON {SCHEMA}.{table}")
        op.execute(f"ALTER TABLE {SCHEMA}.{table} DISABLE ROW LEVEL SECURITY")
    for index, table in (
        ("ix_shot_planning_shot_visual_plans_shot_id", "shot_visual_plans"),
        ("ix_shot_planning_shot_specs_shot_id", "shot_specs"),
        ("ix_shot_planning_shot_specs_shot_revision_id", "shot_specs"),
    ):
        op.drop_index(index, table_name=table, schema=SCHEMA)
    for table in ("shot_specs", "shot_visual_plans", "constraint_packs"):
        for suffix in ("project_id", "organization_id", "tenant_id"):
            op.drop_index(
                f"ix_shot_planning_{table}_{suffix}",
                table_name=table,
                schema=SCHEMA,
            )
    for table in ("constraint_packs", "shot_visual_plans", "shot_specs"):
        op.drop_table(table, schema=SCHEMA)
