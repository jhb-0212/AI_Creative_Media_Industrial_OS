"""镜头规划域：shot_planning schema + 角色 + shot_drafts/shot_revisions（RLS）。

Revision ID: b1a2c3d4e5f6
Revises: a2b3c4d5e6f7
Create Date: 2026-11-17

背景（ADR-R27 镜头域独立）：
镜头域（Shot / ShotDraft / ShotRevision）从 project_planning 正式拆分为独立
``shot_planning`` 域：独立数据所有权、独立 schema + 数据库角色、独立领域模型。
本迁移落地 Phase 1 主链的前两个对象——AI 拆镜草案（ShotDraft）与正式镜头
修订（ShotRevision），并兑现 ADR-R07 的契约债务（草案对象契约已补，见 A4）。

三条必须同时成立的结构约束（对齐 ADR-R16 与 e8f9a0b1c2d3 范式）：
1. 独立 schema ``shot_planning``：与 project_planning 完全隔离，项目域角色写不了
   镜头表，镜头域角色写不了项目表（越权写入在数据库层被拒）。
2. 独立角色 ``svc_shot_planning``：AI/Agent 只经 ``SHOT.DRAFT.CREATE`` 写草案，
   不能写正式镜头修订表（shot_revisions 只授人类确认路径对应的应用层写权，
   数据库层再用「Agent 不得写正式修订」的授权边界兜底）。
3. 状态机下沉：草案 DRAFT→CONFIRMED/SUPERSEDED、修订绑定 Shot 版本递增不覆盖，
   转移表对应用角色只读，「加一条边自我放行」在数据库层被关掉。

授权口径：
- shot_drafts：INSERT（AI 产草案）+ SELECT + 状态列 UPDATE（确认/取代）；
- shot_revisions：INSERT + SELECT（人类确认产出修订；内容列 append-only，
  不授 UPDATE，修订即新行，版本递增不覆盖）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from packages.shared.config import get_settings
from packages.shared.db_roles import DatabaseDomain, role_for

# revision identifiers, used by Alembic.
revision: str = "b1a2c3d4e5f6"
down_revision: str | Sequence[str] | None = "a2b3c4d5e6f7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "shot_planning"
OUTBOX_SCHEMA = "outbox"
ALEMBIC_VERSION = "public.alembic_version"

_ORG_SETTING = "app.current_org_id"
_SCOPE_SETTING = "app.current_projects"

#: 契约声明的草案状态取值（迁移是历史快照，不 import 代码）。
_DRAFT_STATUS_VALUES = ("DRAFT", "CONFIRMED", "SUPERSEDED")


def _scope_expression(column: str) -> str:
    """项目归属判定表达式（与 e7f8a9b0c1d2 的策略文本保持同一口径）。"""
    return f"""(
        organization_id = NULLIF(current_setting('{_ORG_SETTING}', true), '')::uuid
        AND (
            current_setting('{_SCOPE_SETTING}', true) = '*'
            OR {column} = ANY (
                string_to_array(
                    NULLIF(current_setting('{_SCOPE_SETTING}', true), ''), ','
                )
            )
        )
    )"""


def _create_role(role: str) -> None:
    """创建域角色（幂等：已存在则同步口令，不改变其权限）。"""
    password = get_settings().postgres_app_password.get_secret_value()
    if not password:
        raise RuntimeError(
            "POSTGRES_APP_PASSWORD 未配置：拒绝以空口令创建业务域角色"
        )
    escaped = password.replace("'", "''")
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
                CREATE ROLE {role} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
                    NOBYPASSRLS PASSWORD '{escaped}';
            ELSE
                ALTER ROLE {role} WITH PASSWORD '{escaped}';
            END IF;
        END
        $$
        """
    )


def _enable_rls(table: str, project_column: str) -> None:
    """给表加租户隔离 + 项目范围（限制性）策略，与既有项目级资源表同口径。"""
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


def upgrade() -> None:
    role = role_for(DatabaseDomain.SHOT_PLANNING)

    op.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
    _create_role(role)
    op.execute(f"GRANT USAGE ON SCHEMA {SCHEMA} TO {role}")

    # ---- shot_drafts：AI 拆镜草案 ------------------------------------
    op.create_table(
        "shot_drafts",
        # C.3.1 通用实体信封列
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
        # ShotDraft 业务列（契约字段）
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("episode_ref", sa.String(64), nullable=False),
        sa.Column("scene_ref", sa.String(64), nullable=False),
        sa.Column("shot_code", sa.String(64), nullable=False),
        sa.Column("intent_summary", sa.Text(), nullable=False),
        sa.Column("shot_description", sa.Text(), nullable=False),
        # 来源主体类型（agent / human）：把「Agent 只产草案」压到数据层。
        sa.Column("source", sa.String(16), nullable=False),
        sa.CheckConstraint(
            f"status IN ({', '.join(repr(v) for v in _DRAFT_STATUS_VALUES)})",
            name="ck_shot_planning_shot_drafts_status_registered",
        ),
        sa.CheckConstraint(
            "source IN ('agent', 'human')",
            name="ck_shot_planning_shot_drafts_source_registered",
        ),
        schema=SCHEMA,
    )

    # ---- shot_revisions：正式镜头内容修订 ---------------------------
    op.create_table(
        "shot_revisions",
        # C.3.1 通用实体信封列
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
        # ShotRevision 业务列（契约字段）
        sa.Column("shot_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("episode_ref", sa.String(64), nullable=False),
        sa.Column("scene_ref", sa.String(64), nullable=False),
        sa.Column("intent_summary", sa.Text(), nullable=False),
        sa.Column("shot_description", sa.Text(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        schema=SCHEMA,
    )

    # 索引：tenant_id / organization_id（与 EntityMixin 的 index=True 命名一致）。
    op.create_index(
        "ix_shot_planning_shot_drafts_tenant_id", "shot_drafts", ["tenant_id"], schema=SCHEMA
    )
    op.create_index(
        "ix_shot_planning_shot_drafts_organization_id",
        "shot_drafts",
        ["organization_id"],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_shot_planning_shot_drafts_project_id",
        "shot_drafts",
        ["project_id"],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_shot_planning_shot_revisions_tenant_id",
        "shot_revisions",
        ["tenant_id"],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_shot_planning_shot_revisions_organization_id",
        "shot_revisions",
        ["organization_id"],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_shot_planning_shot_revisions_shot_id",
        "shot_revisions",
        ["shot_id"],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_shot_planning_shot_revisions_project_id",
        "shot_revisions",
        ["project_id"],
        schema=SCHEMA,
    )
    # 唯一约束：同 shot 内 revision 唯一（版本递增不覆盖）。
    op.create_index(
        "uq_shot_planning_shot_revisions_shot_revision",
        "shot_revisions",
        ["shot_id", "revision"],
        unique=True,
        schema=SCHEMA,
    )

    # RLS：租户隔离 + 项目范围（限制性）。
    _enable_rls("shot_drafts", "project_id")
    _enable_rls("shot_revisions", "project_id")

    # 授权：本域角色可读、可插入；shot_drafts 允许 UPDATE 状态推进（确认/取代），
    # shot_revisions 是 append-only 内容（修订即新行，不授 UPDATE）。
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON {SCHEMA}.shot_drafts TO {role}")
    op.execute(f"GRANT SELECT, INSERT ON {SCHEMA}.shot_revisions TO {role}")
    op.execute(f"GRANT USAGE ON SCHEMA {OUTBOX_SCHEMA} TO {role}")
    op.execute(f"GRANT SELECT, INSERT ON {OUTBOX_SCHEMA}.outbox_events TO {role}")
    op.execute(f"GRANT SELECT ON {ALEMBIC_VERSION} TO {role}")


def downgrade() -> None:
    role = role_for(DatabaseDomain.SHOT_PLANNING)
    for table in ("shot_drafts", "shot_revisions"):
        op.execute(f"REVOKE ALL ON {SCHEMA}.{table} FROM {role}")
    op.execute(f"REVOKE ALL ON {OUTBOX_SCHEMA}.outbox_events FROM {role}")
    op.execute(f"REVOKE ALL ON {ALEMBIC_VERSION} FROM {role}")
    for table in ("shot_drafts", "shot_revisions"):
        for policy in (f"project_scope_{table}", f"tenant_isolation_{table}"):
            op.execute(f"DROP POLICY IF EXISTS {policy} ON {SCHEMA}.{table}")
        op.execute(f"ALTER TABLE {SCHEMA}.{table} DISABLE ROW LEVEL SECURITY")
    op.drop_index(
        "uq_shot_planning_shot_revisions_shot_revision",
        table_name="shot_revisions",
        schema=SCHEMA,
    )
    for index, table in (
        ("ix_shot_planning_shot_revisions_project_id", "shot_revisions"),
        ("ix_shot_planning_shot_revisions_shot_id", "shot_revisions"),
        ("ix_shot_planning_shot_revisions_organization_id", "shot_revisions"),
        ("ix_shot_planning_shot_revisions_tenant_id", "shot_revisions"),
        ("ix_shot_planning_shot_drafts_project_id", "shot_drafts"),
        ("ix_shot_planning_shot_drafts_organization_id", "shot_drafts"),
        ("ix_shot_planning_shot_drafts_tenant_id", "shot_drafts"),
    ):
        op.drop_index(index, table_name=table, schema=SCHEMA)
    op.drop_table("shot_revisions", schema=SCHEMA)
    op.drop_table("shot_drafts", schema=SCHEMA)
    op.execute(f"REVOKE USAGE ON SCHEMA {SCHEMA} FROM {role}")
    op.execute(f"DROP SCHEMA IF EXISTS {SCHEMA}")
    # 角色保留（DROP ROLE 会因其它对象依赖而失败，且重建角色需重新授权）。
