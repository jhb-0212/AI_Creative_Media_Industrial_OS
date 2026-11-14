"""create asset_service schema: asset_versions + qc_reports (+RLS/grants)

为什么现在建：
- 资产域（AssetVersion/QCReport）契约已齐（object_schemas 有定义、action
  registry 有 CREATE/APPROVE/QC.REPORT.CREATE、state_machines 有主链），
  但运行时为零。建表是本轮「契约→实现→持久化」闭环的第一块物理落点。

结构决策：
- 独立 schema ``asset_service``：数据所有权归资产域，不写进 project_planning。
- asset_versions 只有 entity_id 一个主键：契约的 asset_version_id 与 entity_id
  同值，序列化时派生；不建两个同值主键列（那是漂移面，不是约束力）。
- qc_reports 以 FK 指向 asset_versions(entity_id)：把「报告挂在不存在资产上」
  的悬空引用按死在数据库层，而不是只靠服务层查询。
- 两表都启用 RLS（tenant 隔离），与 decisions 5 一致；app_role 需 USAGE +
  读写授权，否则应用角色 NOBYPASSRLS 会直接 permission denied。

Revision ID: c1d2e3f4a5b6
Revises: b8c9d1e2f3a4
Create Date: 2026-11-14
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c1d2e3f4a5b6"
down_revision: str | Sequence[str] | None = "b8c9d1e2f3a4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "asset_service"
APP_ROLE = "app_role"

_TABLES = ("asset_versions", "qc_reports")


def upgrade() -> None:
    """建 schema / 两表 / RLS 策略 / 应用角色授权。"""
    op.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")

    op.create_table(
        "asset_versions",
        # C.3.1 通用实体信封列
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
        # 资产业务列（契约 AssetVersion）
        sa.Column("asset_type", sa.String(32), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("storage_ref", postgresql.JSONB(), nullable=False),
        sa.Column("source_asset_version_ids", postgresql.JSONB(), nullable=False),
        sa.Column("rights_manifest_ref", sa.String(64), nullable=True),
        sa.Column("adapter_id", sa.String(64), nullable=True),
        sa.Column("tool_product", sa.String(64), nullable=True),
        sa.Column("operator_ref", sa.String(64), nullable=True),
        sa.Column("version", sa.String(32), nullable=True),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_asset_service_asset_versions_tenant_id",
        "asset_versions",
        ["tenant_id"],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_asset_service_asset_versions_type_sha256",
        "asset_versions",
        ["asset_type", "sha256"],
        schema=SCHEMA,
    )

    op.create_table(
        "qc_reports",
        sa.Column("qc_report_id", sa.String(64), primary_key=True),
        sa.Column(
            "asset_version_id",
            sa.String(64),
            sa.ForeignKey(f"{SCHEMA}.asset_versions.entity_id"),
            nullable=False,
        ),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("quality_profile_version", sa.String(64), nullable=False),
        sa.Column("benchmark_version", sa.String(64), nullable=True),
        sa.Column("detector_versions", postgresql.JSONB(), nullable=False),
        sa.Column("findings", postgresql.JSONB(), nullable=False),
        sa.Column("deviation_class", sa.String(24), nullable=True),
        sa.Column("intent_match_status", sa.String(24), nullable=True),
        sa.Column("human_decision", sa.String(32), nullable=True),
        sa.Column("created_by", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_asset_service_qc_reports_asset_version_id",
        "qc_reports",
        ["asset_version_id"],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_asset_service_qc_reports_tenant_id",
        "qc_reports",
        ["tenant_id"],
        schema=SCHEMA,
    )

    # RLS：未注入租户的会话看不到任何行（fail-closed）。
    for table in _TABLES:
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

    # 授权：应用角色（NOBYPASSRLS）必须有 schema USAGE 与表级读写权限。
    op.execute(f"GRANT USAGE ON SCHEMA {SCHEMA} TO {APP_ROLE}")
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {SCHEMA} "
        f"TO {APP_ROLE}"
    )
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA {SCHEMA} "
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {APP_ROLE}"
    )


def downgrade() -> None:
    """回滚：先删策略与表，再回收 schema 授权。"""
    for table in _TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation_{table} ON {SCHEMA}.{table}")
    op.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA {SCHEMA} FROM {APP_ROLE}")
    op.execute(f"REVOKE USAGE ON SCHEMA {SCHEMA} FROM {APP_ROLE}")
    op.drop_table("qc_reports", schema=SCHEMA)
    op.drop_table("asset_versions", schema=SCHEMA)
    op.execute(f"DROP SCHEMA IF EXISTS {SCHEMA}")
