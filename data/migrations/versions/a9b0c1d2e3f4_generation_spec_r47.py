"""ADR-R47 P2：generation_specs 冻结表 + generation_tasks 规格绑定列

本迁移是 P2 的 DDL 事实源，与 `services/production_planning/repository/orm.py` 逐列
一致（`alembic check` 保证无漂移）。三件事一起做，因为它们互为前提：

1. 新表 ``production_planning.generation_specs``：内容寻址的不可变快照，只有
   SELECT/INSERT，没有 UPDATE/DELETE（改写规格等于改写任务执行依据）；
2. ``generation_tasks.generation_spec_id``（可空）+ 复合外键 + 绑定等式 CHECK：
   内联任务必须是 ``inline:%`` 且无规格引用；规格任务必须恰好是 ``genspec:<id>``；
3. RLS 与授权：规格表按项目作用域可见（租户谓词 + 父项目存在性），授权只经
   ``db_grants`` 矩阵生成，迁移不手写 GRANT 字面量。

为什么 CHECK 用 ``NOT VALID``
----------------------------
升级库可能已有历史内联任务行，其 ``prompt_ref`` 未必满足新等式。``NOT VALID``
让约束只对新写入生效，历史行不被追溯判定——**不**等于放弃约束：新行仍受同一
CHECK 拒绝，且后续可用 ``VALIDATE CONSTRAINT`` 在数据清理后收紧。把它写成
「先删历史行再建约束」才是真正危险的。

Revision ID: a9b0c1d2e3f4
Revises: f2b3c4d5e6a7
Create Date: 2026-11-29
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

from packages.shared.db_grants import PRODUCTION_PLANNING, converge_statements
from packages.shared.db_roles import domain_role
from packages.shared.migration_identity import assert_role_present
from packages.shared.rls import tenant_scope_predicate
from packages.shared.status_constraint import status_check
from services.project_planning.domain.enums import NoLifecycleStatus

revision: str = "a9b0c1d2e3f4"
down_revision: str | Sequence[str] | None = "f2b3c4d5e6a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCHEMA = "production_planning"
_SPECS = "generation_specs"
_TASKS = "generation_tasks"

#: 本迁移的冻结授权范围：域内四表 + 平台共享表 + 跨域列级只读的目标。
#: 迁移是时间胶囊，必须显式给出快照，不能读「当前矩阵」决定「那时有什么表」。
_GRANT_SCOPE: frozenset[str] = frozenset(
    {
        "artifact_manifests",
        "production_routes",
        "generation_tasks",
        _SPECS,
        "status_transitions",
        "outbox_events",
        "revoked_tokens",
        "alembic_version",
        "projects",
        "shot_specs",
    }
)


def _assert_domain_role(role: str) -> None:
    assert_role_present(role, op=op)


def upgrade() -> None:
    _assert_domain_role(domain_role(PRODUCTION_PLANNING))

    op.create_table(
        _SPECS,
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
        sa.Column("generation_spec_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("shot_revision_id", sa.String(64), nullable=False),
        sa.Column("shot_spec_id", sa.String(64), nullable=False),
        sa.Column("production_route_ref", sa.String(64), nullable=False),
        sa.Column("constraint_snapshot_id", sa.String(128), nullable=False),
        sa.Column("template_version", sa.String(64), nullable=False),
        sa.Column("derivation_version", sa.String(64), nullable=False),
        sa.Column("size_policy_id", sa.String(64), nullable=False),
        sa.Column("size_policy_version", sa.String(64), nullable=False),
        sa.Column("model_id", sa.String(128), nullable=False),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("generation_parameters", JSONB, nullable=False, server_default="{}"),
        sa.Column("prompt_text", sa.Text(), nullable=False),
        sa.Column("negative_prompt", sa.Text(), nullable=True),
        sa.Column("media_type", sa.String(64), nullable=False),
        sa.Column("size", sa.String(32), nullable=False),
        sa.Column("aspect_ratio", sa.String(32), nullable=False),
        sa.Column("spec_sha256", sa.String(64), nullable=False),
        sa.Column("prompt_sha256", sa.String(64), nullable=False),
        # 同一租户内规格 id 唯一；projects 归属用复合外键（project_id + tenant_id）。
        sa.UniqueConstraint(
            "tenant_id", "generation_spec_id", name="uq_generation_specs_spec_tenant"
        ),
        sa.UniqueConstraint(
            "project_id", "entity_id", name="uq_generation_specs_entity_project"
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "tenant_id"],
            [
                "project_planning.projects.entity_id",
                "project_planning.projects.tenant_id",
            ],
            name="fk_generation_specs_project_tenant",
        ),
        sa.CheckConstraint(
            "size ~ '^[1-9][0-9]{2,4}x[1-9][0-9]{2,4}$'",
            name="ck_generation_specs_size_shape",
        ),
        sa.CheckConstraint(
            "aspect_ratio ~ '^[1-9][0-9]{0,2}:[1-9][0-9]{0,2}$'",
            name="ck_generation_specs_aspect_shape",
        ),
        sa.CheckConstraint(
            "media_type IN ('audio/wav', 'image/jpeg', 'image/png', 'image/webp', "
            "'video/mp4', 'video/webm')",
            name="ck_generation_specs_media_type",
        ),
        status_check(
            (NoLifecycleStatus.NO_LIFECYCLE.value,), table=_SPECS
        ),
        schema=_SCHEMA,
    )
    op.create_index(
        "ix_production_planning_generation_specs_project_id",
        _SPECS,
        ["project_id"],
        schema=_SCHEMA,
    )
    op.create_index(
        "ix_production_planning_generation_specs_tenant_id",
        _SPECS,
        ["tenant_id"],
        schema=_SCHEMA,
    )
    op.create_index(
        "ix_production_planning_generation_specs_shot_spec_id",
        _SPECS,
        ["shot_spec_id"],
        schema=_SCHEMA,
    )

    op.execute(f"ALTER TABLE {_SCHEMA}.{_SPECS} ENABLE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation_{_SPECS} ON {_SCHEMA}.{_SPECS} "
        f"USING ({tenant_scope_predicate()} AND EXISTS ("
        f"SELECT 1 FROM project_planning.projects p "
        f"WHERE p.entity_id = {_SCHEMA}.{_SPECS}.project_id "
        f"AND p.tenant_id = {_SCHEMA}.{_SPECS}.tenant_id))"
    )
    op.execute(f"ALTER TABLE {_SCHEMA}.{_SPECS} FORCE ROW LEVEL SECURITY")

    # 任务侧绑定列 + 约束。列可空：历史内联任务没有规格引用。
    op.add_column(
        _TASKS,
        sa.Column("generation_spec_id", sa.String(64), nullable=True),
        schema=_SCHEMA,
    )
    op.create_index(
        "ix_production_planning_generation_tasks_generation_spec_id",
        _TASKS,
        ["generation_spec_id"],
        schema=_SCHEMA,
    )
    op.execute(
        f"ALTER TABLE {_SCHEMA}.{_TASKS} ADD CONSTRAINT "
        "ck_generation_tasks_prompt_ref_spec_binding CHECK ("
        "(prompt_ref LIKE 'inline:%' AND generation_spec_id IS NULL) "
        "OR (generation_spec_id IS NOT NULL AND prompt_ref = 'genspec:' || generation_spec_id)"
        ") NOT VALID"
    )
    op.create_foreign_key(
        "fk_generation_tasks_generation_spec",
        _TASKS,
        _SPECS,
        ["tenant_id", "generation_spec_id"],
        ["tenant_id", "generation_spec_id"],
        source_schema=_SCHEMA,
        referent_schema=_SCHEMA,
        ondelete="RESTRICT",
    )

    for statement in converge_statements(
        domain_role(PRODUCTION_PLANNING), PRODUCTION_PLANNING, tables=_GRANT_SCOPE
    ):
        op.execute(statement)


def downgrade() -> None:
    """对称回滚：删列/约束/表。已冻结的规格行随表消失——本迁移不回填。"""
    op.drop_constraint(
        "fk_generation_tasks_generation_spec", _TASKS, schema=_SCHEMA, type_="foreignkey"
    )
    op.execute(
        f"ALTER TABLE {_SCHEMA}.{_TASKS} DROP CONSTRAINT ck_generation_tasks_prompt_ref_spec_binding"
    )
    op.drop_index(
        "ix_production_planning_generation_tasks_generation_spec_id",
        table_name=_TASKS,
        schema=_SCHEMA,
    )
    op.drop_column(_TASKS, "generation_spec_id", schema=_SCHEMA)

    op.execute(f"DROP POLICY IF EXISTS tenant_isolation_{_SPECS} ON {_SCHEMA}.{_SPECS}")
    op.execute(f"ALTER TABLE {_SCHEMA}.{_SPECS} DISABLE ROW LEVEL SECURITY")
    for index in (
        "ix_production_planning_generation_specs_shot_spec_id",
        "ix_production_planning_generation_specs_tenant_id",
        "ix_production_planning_generation_specs_project_id",
    ):
        op.drop_index(index, table_name=_SPECS, schema=_SCHEMA)
    op.drop_table(_SPECS, schema=_SCHEMA)

    # 授权收敛回「没有新表」的矩阵：显式给出回滚快照，不能读当前矩阵。
    for statement in converge_statements(
        domain_role(PRODUCTION_PLANNING),
        PRODUCTION_PLANNING,
        tables=frozenset(_GRANT_SCOPE - {_SPECS}),
    ):
        op.execute(statement)
