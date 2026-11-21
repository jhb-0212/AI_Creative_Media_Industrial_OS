"""ShotSpec 运行时落地：建表 + SS-3 库层约束 + 组织 RLS（A22）

为什么现在建这张表
------------------------------------------------------------
A21 只补了契约：状态机（DRAFT→LOCKED）、`SHOTSPEC.CREATE`，并把两条动作登记为
not_implemented。契约先行是刻意的——但契约里的「同一修订至多一份规格」「切点
单调」「绑定精确修订」在库里没有任何对应物，等于三条不变量只活在文档里。

本迁移把三条同时落到最可靠层：

1. **同一修订至多一份规格** → ``uq_shot_spec_per_revision (shot_revision_id)``。
   服务层「先查后插」在并发下必被穿透（两边都查到没有），唯一约束才是唯一能
   真正拒绝的东西。
2. **绑定精确修订且同项目** → 复合外键
   ``(project_id, shot_revision_id) → shot_revisions(project_id, entity_id)``。
   单列外键只能证明修订存在，无法阻止「规格属项目 A、修订属项目 B」
   （负测 ACT-SPEC-NEG-002 打的正是这条）。为此先给 shot_revisions 补引用目标
   唯一键 ``uq_shot_revisions_entity_project``。
3. **SS-3 切点单调与帧数换算** → ``ck_shot_specs_cut_points``。递增性用
   IMMUTABLE 函数表达：CHECK 里不允许子查询，而「任意长度数组严格递增」没有
   纯表达式写法。末点上限直接在 CHECK 里做整数除法，与领域层
   ``shot_spec.total_frames()`` 同一算式（都是正数截断），避免「库里合法、
   领域拒绝」这种最难查的分歧。

为什么函数放在 project_planning schema
------------------------------------------------------------
它服务于本 schema 的一张表，随表一起存在与消亡；放进 public 会让「谁拥有它」
变成没人回答的问题，也会让其他域误以为那是通用能力（它不是：它只判一种
数组语义）。downgrade 先删表再删函数——CHECK 依赖函数，顺序反了会失败。

权限与 RLS
------------------------------------------------------------
与 a13b0 同法：不手写 GRANT，调用 ``converge_statements(..., tables={"shot_specs"})``
由矩阵生成；策略同样用父项目存在性表达组织规则（子表不冗余 organization_id，
避免项目换组织时产生第二事实源）。

幂等与 fail-closed
------------------------------------------------------------
``uq_shot_revisions_entity_project`` 可能在混合残留库已被建过：先按 catalog 实判，
同名且列集等价则复用，列集不等价则拒绝升级——静默接受一个语义不同的约束，
比中止迁移更坏（后续复合外键会引用一个错误的目标键）。

Revision ID: c3d4e5f6a7b8
Revises: b1c2d3e4f5a6
Create Date: 2026-11-21
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
from services.project_planning.domain.enums import ShotSpecStatus

revision: str = "c3d4e5f6a7b8"
down_revision: str | Sequence[str] | None = "b1c2d3e4f5a6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCHEMA = PROJECT_PLANNING
_TABLE = "shot_specs"
_CUT_POINTS_FN = "cut_points_monotonic"
_REVISION_UQ = "uq_shot_revisions_entity_project"


def _row_visibility(table: str) -> str:
    """双维度可见性谓词（租户 + 组织），与 a13b0 同源、同形。

    组织维度经父项目存在性表达（子表不冗余 organization_id）。相关条件
    ``p.tenant_id = <本表>.tenant_id`` 不能省——丢了它，谓词退化成「当前租户里
    存在任意可见项目」即真，他组织的行会整片放行，而只查 ``EXISTS`` 子串的
    判据照样报 PASS。
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
    """C.3.1 信封列（与 packages/shared/orm.py::EntityMixin 逐列一致）。

    不抽公共函数到迁移之外：迁移是时间胶囊，必须在未来代码演进后仍准确描述
    「这一刻的表长什么样」。
    """
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


def _ensure_revision_unique_target() -> None:
    """确保 shot_revisions 上存在 (project_id, entity_id) 唯一键且语义等价。

    fail-closed：同名约束存在但列集不同 → 拒绝升级。列顺序不参与比较（唯一性
    对列顺序不敏感，外键引用只要求列集唯一）。
    """
    existing = (
        op.get_bind()
        .execute(
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
            {"rel": f"{_SCHEMA}.shot_revisions", "name": _REVISION_UQ},
        )
        .scalars()
        .all()
    )
    wanted = {"project_id", "entity_id"}
    if existing and set(existing) != wanted:
        raise RuntimeError(
            f"{_REVISION_UQ} 已存在但列集不等价：实际={sorted(existing)}，"
            f"期望={sorted(wanted)}；拒绝在不兼容约束上建外键"
        )
    if existing:
        return
    op.create_unique_constraint(
        _REVISION_UQ, "shot_revisions", ["project_id", "entity_id"], schema=_SCHEMA
    )


def _create_cut_points_function() -> None:
    """SS-3 的递增性与首点判定（CHECK 里不能写子查询，故封装成 IMMUTABLE 函数）。

    ``COALESCE(..., false)`` 不能省：空数组时 ``points[1] = 0`` 求值为 NULL，
    而 CHECK 把 NULL 当作通过——那会让「空切点」变成合法值，正好绕开 SS-3。
    同样的理由适用于 ``array_length`` 为 NULL 的情形。
    """
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {_SCHEMA}.{_CUT_POINTS_FN}(points integer[])
        RETURNS boolean
        LANGUAGE sql
        IMMUTABLE
        AS $$
            SELECT COALESCE(array_length(points, 1), 0) >= 1
               AND COALESCE(points[1] = 0, false)
               AND NOT EXISTS (
                   SELECT 1
                   FROM generate_subscripts(points, 1) AS i
                   WHERE i > 1 AND points[i] <= points[i - 1]
               )
        $$
        """
    )


def upgrade() -> None:
    # 1. 复合外键的引用目标（幂等 + fail-closed）。
    _ensure_revision_unique_target()

    # 2. SS-3 的库层函数（先建函数，建表时的 CHECK 会引用它）。
    _create_cut_points_function()

    # 3. 表本体。
    op.create_table(
        _TABLE,
        *_envelope_columns(),
        sa.Column("shot_spec_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("shot_revision_id", sa.String(64), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("fps_num", sa.Integer(), nullable=False),
        sa.Column("fps_den", sa.Integer(), nullable=False),
        sa.Column("cut_points_frames", postgresql.ARRAY(sa.Integer()), nullable=False),
        sa.Column("aspect_ratio", sa.String(32), nullable=False),
        sa.Column("coordinate_system", sa.String(32), nullable=False),
        sa.Column("camera_path", sa.Text(), nullable=False),
        sa.Column("subject_paths", sa.Text(), nullable=False),
        sa.Column("shot_size_changes", sa.String(500), nullable=False),
        sa.Column("previz_ref", sa.String(200), nullable=False),
        sa.Column("control_signal_refs", postgresql.JSONB(), nullable=False),
        sa.Column("locked_dimensions", sa.String(512), nullable=False),
        # 同一修订至多一份规格（SHOTSPEC.CREATE 的审批要求）。
        sa.UniqueConstraint("shot_revision_id", name="uq_shot_spec_per_revision"),
        sa.UniqueConstraint(
            "project_id", "entity_id", name="uq_shot_specs_entity_project"
        ),
        # 复合外键：跨项目绑定在库层即被拒（ACT-SPEC-NEG-002）。
        sa.ForeignKeyConstraint(
            ["project_id", "shot_revision_id"],
            [
                f"{_SCHEMA}.shot_revisions.project_id",
                f"{_SCHEMA}.shot_revisions.entity_id",
            ],
            name="fk_shot_specs_revision",
        ),
        sa.CheckConstraint(
            "shot_spec_id = entity_id", name="ck_shot_specs_identity_single"
        ),
        sa.CheckConstraint("duration_ms >= 1", name="ck_shot_specs_duration_positive"),
        sa.CheckConstraint(
            "fps_num >= 1 AND fps_den >= 1", name="ck_shot_specs_fps_positive"
        ),
        sa.CheckConstraint(
            f"{_SCHEMA}.{_CUT_POINTS_FN}(cut_points_frames) "
            "AND cut_points_frames[array_length(cut_points_frames, 1)] "
            "< (duration_ms * fps_num) / (1000 * fps_den)",
            name="ck_shot_specs_cut_points",
        ),
        status_check((s.value for s in ShotSpecStatus), table=_TABLE),
        schema=_SCHEMA,
    )
    op.create_index(
        op.f("ix_project_planning_shot_specs_tenant_id"),
        _TABLE,
        ["tenant_id"],
        schema=_SCHEMA,
    )
    op.create_index(
        op.f("ix_project_planning_shot_specs_project_id"),
        _TABLE,
        ["project_id"],
        schema=_SCHEMA,
    )

    # 4. 行级安全：表一诞生就带策略，不留「以后补」的口子。
    op.execute(f"ALTER TABLE {_SCHEMA}.{_TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation_{_TABLE} "
        f"ON {_SCHEMA}.{_TABLE} " + _row_visibility(_TABLE)
    )
    op.execute(f"ALTER TABLE {_SCHEMA}.{_TABLE} FORCE ROW LEVEL SECURITY")

    # 5. 授权：由矩阵生成（不手写 GRANT，避免「权限面由表清单决定」）。
    role = domain_role(_SCHEMA)
    for statement in converge_statements(role, _SCHEMA, tables={_TABLE}):
        op.execute(statement)


def downgrade() -> None:
    _drop_table()


def _drop_table() -> None:
    # 先撤策略再删表：删表会连带删策略，但显式撤销让回滚文本自己说清
    # 「这张表曾经带过什么安全属性」，而不是依赖 DDL 的副作用。
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation_{_TABLE} ON {_SCHEMA}.{_TABLE}")
    op.execute(f"ALTER TABLE {_SCHEMA}.{_TABLE} DISABLE ROW LEVEL SECURITY")

    op.drop_index(
        op.f("ix_project_planning_shot_specs_project_id"),
        table_name=_TABLE,
        schema=_SCHEMA,
    )
    op.drop_index(
        op.f("ix_project_planning_shot_specs_tenant_id"),
        table_name=_TABLE,
        schema=_SCHEMA,
    )
    op.drop_table(_TABLE, schema=_SCHEMA)

    # 表删掉之后函数才无引用者。顺序反了 PG 会以「被 CHECK 依赖」拒绝。
    op.execute(f"DROP FUNCTION IF EXISTS {_SCHEMA}.{_CUT_POINTS_FN}(integer[])")

    # 刻意不删除 uq_shot_revisions_entity_project：ShotRevisionModel 本就声明了
    # 它，删除会让降级后的库与领域模型不一致；而且它本身是正确的领域约束
    # （「修订的同项目身份」），删了属反向破坏。
