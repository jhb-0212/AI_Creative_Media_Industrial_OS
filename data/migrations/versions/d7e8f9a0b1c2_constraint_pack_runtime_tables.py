"""ConstraintPack 运行时落地：建表 + 7.9 库层不变量 + 组织 RLS（A23）

为什么现在建这张表
------------------------------------------------------------
7.9 给了 ConstraintPack 的完整字段契约（包级 12 字段 + 条目 8 必填字段）、
五个状态与四条边，并在 31.6 登记了 ``CONSTRAINT.PACK.CREATE`` / ``LOCK``。
但在此之前，库里没有任何对应物——「无锁定约束必须显式标 NOT_APPLICABLE」
「只有 ACTIVE 可作生成输入」「条目必须有审批引用」三条都只活在文档里。

本迁移把「绕过服务的写入会破坏哪条契约」逐条落到最可靠层：

1. **包与项目同租户** → ``fk_constraint_packs_project_tenant`` 复合外键。
   单列 ``project_id`` 只能证明项目存在，挡不住「包属租户 A、项目属租户 B」。
2. **同版本内唯一 / 版本推进不覆盖** → ``uq_constraint_pack_version``。
3. **同一逻辑包至多一个 ACTIVE 版本** → 部分唯一索引
   ``uq_constraint_pack_single_active WHERE status = 'ACTIVE'``。7.9「只有 ACTIVE
   可作为生成输入」要求「哪一份生效」有唯一答案；两个 ACTIVE 版本并存会让下游
   按 pack 查询拿到两份互斥的锁定维度。
4. **锁定维度受控** → ``ck_constraint_packs_dimensions_known``（``<@`` 包含于
   契约列举的七个值）。允许空数组：NOT_APPLICABLE 的包没有维度。
5. **条目八字段必填** → ``ck_constraint_packs_entries`` + IMMUTABLE 函数
   ``constraint_entries_complete``。CHECK 不能写子查询，而「遍历 JSONB 数组逐条
   判八字段」没有纯表达式写法。
6. **applicability 与内容自洽** → ``ck_constraint_packs_not_applicable_empty``。
   标为「无锁定约束」却列了维度或条目，是自述矛盾。
7. **shot_ids 必须指向同项目的镜头** → 触发器 ``trg_constraint_packs_shots``。
   数组元素无法用外键表达，而「这个镜头在本项目里」是归属不变量，不能只靠
   服务层记得校验。

为什么这里没有 ``ck_constraint_packs_identity_single``（而相邻三张表都有）
------------------------------------------------------------
``scenes`` / ``shots`` / ``shot_revisions`` / ``shot_specs`` 都有「业务 id =
entity_id」的 CHECK（SC-1 / SH-1 / SR-1「身份单一」）。本表刻意没有：那几个对象的
契约要求两者同一；本对象相反——``entity_id`` 是**版本行**标识，
``constraint_pack_id`` 是**逻辑包**标识，7.9 的 ``version`` 写「变更须新建版本并
supersede 旧版本」，新版本就是「新行 + 同包标识 + version 递增」。

补上那条 CHECK 的后果不是多一道防线，而是让上面第 2、3 条约束同时失效：同一行
的两列既然被钉死为同一值，``uq_constraint_pack_version`` 与
``uq_constraint_pack_single_active`` 就只能在「同一行与自己冲突」这个不可能事件上
生效，即两条不变量被登记成 RUNTIME_ENFORCED 却什么也没挡住。恒真的约束比没有约束
更危险——它会让闸门报绿。

为什么 shot 归属用触发器而不是关联表
------------------------------------------------------------
关联表能让复合外键直接生效，但会把契约字段 ``shot_ids`` 降级成派生投影——
库里同时存在「数组」与「关联行」两份事实，任何写入路径都必须维护一致性。
触发器保持「数组是唯一存储形态」，同时在库层做与复合外键同等的判定。
并发上唯一的窗口是「校验通过后镜头被删」：Shot 一旦创建即绑定项目，项目归属
不可迁移，删除路径受 ``fk_shots_scene`` 等约束保护，该窗口不构成新的不变量缺口。

触发器以 ``ERRCODE='23503'`` 抛出，与真外键共用同一错误映射
（``packages/shared/port_errors.classify_write_error`` 按 SQLSTATE 前缀 ``23``
判定），不需要为它新增一条翻译分支——新增分支就意味着又一处「记得同步」。

权限与 RLS
------------------------------------------------------------
与 a13b0 / c3d4e5 同法：不手写 GRANT，调用
``converge_statements(..., tables={"constraint_packs"})`` 由矩阵生成；策略用
父项目存在性表达组织规则（子表不冗余 organization_id）。

Revision ID: d7e8f9a0b1c2
Revises: c3d4e5f6a7b8
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
from services.project_planning.domain.constraint_pack import LOCKED_DIMENSIONS
from services.project_planning.domain.enums import ConstraintPackStatus

revision: str = "d7e8f9a0b1c2"
down_revision: str | Sequence[str] | None = "c3d4e5f6a7b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCHEMA = PROJECT_PLANNING
_TABLE = "constraint_packs"
_UNIQUE_ARRAY_FN = "text_array_unique"
_ENTRIES_FN = "constraint_entries_complete"
_SHOTS_FN = "assert_constraint_pack_shots_in_project"
_SHOTS_TRG = "trg_constraint_packs_shots"
_ACTIVE_INDEX = "uq_constraint_pack_single_active"
_ENTRY_REQUIRED = (
    "constraint_id",
    "target",
    "shot_scope",
    "rule",
    "source",
    "authority_level",
    "valid_at",
    "approval_ref",
)


def _row_visibility(table: str) -> str:
    """双维度可见性谓词（租户 + 组织），与 a13b0 / c3d4e5 同源同形。

    组织维度经父项目存在性表达（子表不冗余 organization_id）。相关条件
    ``p.tenant_id = <本表>.tenant_id`` 不能省——丢了它，谓词退化成「当前租户里
    存在任意可见项目」即真，他组织的行会整片放行。
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


def _create_functions() -> None:
    """建两个 IMMUTABLE 判定函数（CHECK 里不能写子查询，故封装）。

    ``COALESCE(..., false)`` 在每个可空比较上都不能省：空数组时
    ``array_length(...)`` 为 NULL，整个 CHECK 求值为 NULL，而 CHECK 把 NULL
    当作通过——那会让「空集合」变成合法值，正好绕开要守的约束。
    """
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {_SCHEMA}.{_UNIQUE_ARRAY_FN}(items text[])
        RETURNS boolean
        LANGUAGE sql
        IMMUTABLE
        AS $$
            SELECT COALESCE(cardinality(items), 0) = (
                       SELECT count(DISTINCT item) FROM unnest(items) AS item
                   )
               AND NOT EXISTS (
                   SELECT 1 FROM unnest(items) AS item
                   WHERE item IS NULL OR btrim(item) = ''
               )
        $$
        """
    )
    required = ", ".join(f"'{name}'" for name in _ENTRY_REQUIRED)
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {_SCHEMA}.{_ENTRIES_FN}(entries jsonb)
        RETURNS boolean
        LANGUAGE sql
        IMMUTABLE
        AS $$
            SELECT COALESCE(jsonb_typeof(entries) = 'array', false)
               AND NOT EXISTS (
                   SELECT 1
                   FROM jsonb_array_elements(entries) AS entry
                   WHERE jsonb_typeof(entry) <> 'object'
                      OR NOT (entry ?& ARRAY[{required}])
                      OR EXISTS (
                          SELECT 1
                          FROM unnest(ARRAY[{required}]) AS required_key(name)
                          WHERE COALESCE(btrim(entry ->> required_key.name), '') = ''
                      )
               )
               AND (
                   SELECT count(*)
                   FROM jsonb_array_elements(entries) AS entry
               ) = (
                   SELECT count(DISTINCT entry ->> 'constraint_id')
                   FROM jsonb_array_elements(entries) AS entry
               )
        $$
        """
    )


def _create_shots_trigger() -> None:
    """shot_ids 必须指向同项目的镜头（数组元素无法用外键表达的归属不变量）。

    ``ERRCODE = '23503'``：与真外键共用同一错误映射，不需要在
    ``classify_write_error`` 里为它加分支——多一个分支就是多一处「记得同步」。
    """
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {_SCHEMA}.{_SHOTS_FN}()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM unnest(NEW.shot_ids) AS sid
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM {_SCHEMA}.shots AS s
                    WHERE s.entity_id = sid AND s.project_id = NEW.project_id
                )
            ) THEN
                RAISE EXCEPTION
                    'constraint_packs.shot_ids 含不属于项目 % 的镜头', NEW.project_id
                    USING ERRCODE = '23503',
                          CONSTRAINT = 'fk_constraint_packs_shots';
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        f"CREATE TRIGGER {_SHOTS_TRG} "
        f"BEFORE INSERT OR UPDATE OF shot_ids, project_id ON {_SCHEMA}.{_TABLE} "
        f"FOR EACH ROW EXECUTE FUNCTION {_SCHEMA}.{_SHOTS_FN}()"
    )


def upgrade() -> None:
    # 1. 判定函数（建表时 CHECK 会引用它们）。
    _create_functions()

    # 2. 表本体。
    op.create_table(
        _TABLE,
        *_envelope_columns(),
        sa.Column("constraint_pack_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("scene_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("shot_ids", postgresql.ARRAY(sa.String(64)), nullable=False),
        sa.Column(
            "shot_group_ids",
            postgresql.ARRAY(sa.String(64)),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("applicability", sa.String(32), nullable=False),
        sa.Column(
            "locked_dimensions",
            postgresql.ARRAY(sa.String(32)),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("constraints", postgresql.JSONB(), nullable=False),
        sa.Column(
            "source_previz_revision",
            sa.String(200),
            nullable=False,
            server_default="",
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("approved_by", sa.String(64), nullable=False, server_default=""),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        # 包标识 + 版本：7.9「同版本内唯一；变更须新建版本」。
        sa.UniqueConstraint("constraint_pack_id", "version", name="uq_constraint_pack_version"),
        sa.UniqueConstraint("project_id", "entity_id", name="uq_constraint_packs_entity_project"),
        # 包与项目同租户（单列外键挡不住跨租户错配）。
        sa.ForeignKeyConstraint(
            ["project_id", "tenant_id"],
            [
                f"{_SCHEMA}.projects.entity_id",
                f"{_SCHEMA}.projects.tenant_id",
            ],
            name="fk_constraint_packs_project_tenant",
        ),
        sa.CheckConstraint(
            "applicability IN ('APPLICABLE', 'NOT_APPLICABLE')",
            name="ck_constraint_packs_applicability",
        ),
        sa.CheckConstraint(
            "cardinality(shot_ids) >= 1",
            name="ck_constraint_packs_shot_ids_present",
        ),
        sa.CheckConstraint(
            f"{_SCHEMA}.{_UNIQUE_ARRAY_FN}(shot_ids::text[])",
            name="ck_constraint_packs_shot_ids_unique",
        ),
        sa.CheckConstraint(
            f"{_SCHEMA}.{_UNIQUE_ARRAY_FN}(shot_group_ids::text[])",
            name="ck_constraint_packs_shot_groups_unique",
        ),
        sa.CheckConstraint(
            "locked_dimensions::text[] <@ ARRAY["
            + ", ".join(f"'{name}'" for name in sorted(LOCKED_DIMENSIONS))
            + "]::text[]",
            name="ck_constraint_packs_dimensions_known",
        ),
        sa.CheckConstraint(
            f"{_SCHEMA}.{_ENTRIES_FN}(constraints)",
            name="ck_constraint_packs_entries",
        ),
        sa.CheckConstraint(
            "applicability <> 'NOT_APPLICABLE' OR "
            "(cardinality(locked_dimensions) = 0 "
            "AND jsonb_array_length(constraints) = 0)",
            name="ck_constraint_packs_not_applicable_empty",
        ),
        sa.CheckConstraint("version >= 1", name="ck_constraint_packs_version_positive"),
        status_check((s.value for s in ConstraintPackStatus), table=_TABLE),
        schema=_SCHEMA,
    )
    op.create_index(
        op.f("ix_project_planning_constraint_packs_tenant_id"),
        _TABLE,
        ["tenant_id"],
        schema=_SCHEMA,
    )
    op.create_index(
        op.f("ix_project_planning_constraint_packs_project_id"),
        _TABLE,
        ["project_id"],
        schema=_SCHEMA,
    )
    # 同一逻辑包至多一个 ACTIVE 版本（部分唯一）。
    op.create_index(
        _ACTIVE_INDEX,
        _TABLE,
        ["constraint_pack_id"],
        unique=True,
        schema=_SCHEMA,
        postgresql_where=sa.text("status = 'ACTIVE'"),
    )

    # 3. shot_ids 同项目触发器（表建好后才能挂）。
    _create_shots_trigger()

    # 4. 行级安全：表一诞生就带策略，不留「以后补」的口子。
    op.execute(f"ALTER TABLE {_SCHEMA}.{_TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation_{_TABLE} ON {_SCHEMA}.{_TABLE} " + _row_visibility(_TABLE)
    )
    op.execute(f"ALTER TABLE {_SCHEMA}.{_TABLE} FORCE ROW LEVEL SECURITY")

    # 5. 授权：由矩阵生成（不手写 GRANT，避免「权限面由表清单决定」）。
    role = domain_role(_SCHEMA)
    for statement in converge_statements(role, _SCHEMA, tables={_TABLE}):
        op.execute(statement)


def downgrade() -> None:
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation_{_TABLE} ON {_SCHEMA}.{_TABLE}")
    op.execute(f"ALTER TABLE {_SCHEMA}.{_TABLE} DISABLE ROW LEVEL SECURITY")

    op.drop_index(_ACTIVE_INDEX, table_name=_TABLE, schema=_SCHEMA)
    op.drop_index(
        op.f("ix_project_planning_constraint_packs_project_id"),
        table_name=_TABLE,
        schema=_SCHEMA,
    )
    op.drop_index(
        op.f("ix_project_planning_constraint_packs_tenant_id"),
        table_name=_TABLE,
        schema=_SCHEMA,
    )
    # 删表会连带删掉表上的触发器；函数要显式删（它不随表消失）。
    op.drop_table(_TABLE, schema=_SCHEMA)
    op.execute(f"DROP FUNCTION IF EXISTS {_SCHEMA}.{_SHOTS_FN}()")

    # CHECK 依赖这两个函数，表删掉之后它们才无引用者（顺序反了 PG 会拒绝）。
    op.execute(f"DROP FUNCTION IF EXISTS {_SCHEMA}.{_ENTRIES_FN}(jsonb)")
    op.execute(f"DROP FUNCTION IF EXISTS {_SCHEMA}.{_UNIQUE_ARRAY_FN}(text[])")
