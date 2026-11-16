"""交付清单表：delivery_manifests（append-only 版本，RLS，列级最小授权）

Revision ID: d6e7f8a9b0c1
Revises: b3c4d5e6f7a8
Create Date: 2026-11-17

背景（包 D：Phase 1 主链与交付闸门）：
`DELIVERY.MANIFEST.CREATE` 在契约里是 ACTIVE 动作，运行时却没有任何落点。
交付清单不是"一张记录表"，它是对客户的交付依据，三条性质必须在结构层成立：

1. **版本不可变**：契约写「每个 Manifest 版本不可变」。修正只能新建 manifest_id，
   因此本表**没有 UPDATE 路径**——角色只授予 SELECT/INSERT，并加 append-only
   触发器兜住超级用户与迁移角色（与 `qc_reports` 同一口径）。
2. **只引用已成立的证据**：`asset_refs` 指向的资产版本必须存在且已 APPROVED/GOLD，
   由服务层逐个校验；数据库层用 JSONB 存引用，不建外键——交付清单要能保真
   记录"当时引用了哪些版本"，一旦某版本被归档（未来）不该连带删除清单。
3. **可重建**：`checksum` 覆盖可重建内容（引用集合 + 重建配方），不含 id 与
   时间戳，因此"用清单重建一次交付，哈希应与原件一致"（G1）是可验证的。

`project_id` 是契约对象没有、但隔离策略需要的列：五张项目级资源表上的
`project_scope_*` 限制性策略按同一份表达式工作，缺这一列就只能靠租户边界兜底，
同租户跨项目的清单泄漏没有拒绝依据。序列化刻意不输出它（契约没声明）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from packages.shared.db_roles import DatabaseDomain, role_for

# revision identifiers, used by Alembic.
revision: str = "d6e7f8a9b0c1"
down_revision: str | Sequence[str] | None = "b3c4d5e6f7a8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "asset_service"
TABLE = "delivery_manifests"
OUTBOX_SCHEMA = "outbox"
ALEMBIC_VERSION = "public.alembic_version"

_ORG_SETTING = "app.current_org_id"
_SCOPE_SETTING = "app.current_projects"

#: 允许的状态取值（契约 DeliveryManifest 全量；只有 DRAFT 会在本期产生）。
_STATUS_VALUES = (
    "DRAFT",
    "VALIDATING",
    "VALIDATED",
    "RELEASED",
    "INVALID",
    "STALE",
    "REVOKED",
    "SUPERSEDED",
)

_APPEND_ONLY_GUARD = f"""
CREATE OR REPLACE FUNCTION {SCHEMA}.{TABLE}_guard()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION
        '{TABLE} 是 append-only 交付清单，禁止 % （每个 Manifest 版本不可变，'
        '修正须新建 manifest_id）', TG_OP;
END;
$$;
"""


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


def upgrade() -> None:
    op.create_table(
        TABLE,
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
        # 交付清单业务列（契约字段）
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("asset_refs", postgresql.JSONB(), nullable=False),
        sa.Column("checksum", sa.String(64), nullable=False),
        sa.Column("picture_lock_ref", sa.String(64), nullable=False),
        sa.Column("qc_final_ref", sa.String(64), nullable=False),
        sa.Column("rebuild_recipe", sa.Text(), nullable=False),
        sa.Column("rights_evidence_refs", postgresql.JSONB(), nullable=False),
        sa.Column("spec_ref", sa.String(64), nullable=False),
        sa.CheckConstraint(
            f"status IN ({', '.join(repr(v) for v in _STATUS_VALUES)})",
            name="ck_delivery_manifests_status_registered",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_asset_service_delivery_manifests_project",
        TABLE,
        ["project_id"],
        schema=SCHEMA,
    )
    # 索引名与 SQLAlchemy 的自动命名一致（EntityMixin 的 tenant_id /
    # organization_id 都带 index=True）；名字不一致会让 `alembic check` 报
    # "库里多一个、模型少一个"的假漂移。
    op.create_index(
        "ix_asset_service_delivery_manifests_tenant_id",
        TABLE,
        ["tenant_id"],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_asset_service_delivery_manifests_organization_id",
        TABLE,
        ["organization_id"],
        schema=SCHEMA,
    )

    # append-only：先建函数与触发器，再做任何授权——顺序反了会出现"能改已建表"的窗口。
    op.execute(_APPEND_ONLY_GUARD)
    op.execute(
        f"""
        CREATE TRIGGER trg_{TABLE}_guard
        BEFORE UPDATE OR DELETE ON {SCHEMA}.{TABLE}
        FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.{TABLE}_guard()
        """
    )

    # RLS：租户隔离 + 项目范围（限制性），与既有五张项目级资源表同口径。
    op.execute(f"ALTER TABLE {SCHEMA}.{TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY tenant_isolation_{TABLE}
        ON {SCHEMA}.{TABLE}
        USING (
            tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid
        )
        """
    )
    expression = _scope_expression("project_id")
    op.execute(
        f"""
        CREATE POLICY project_scope_{TABLE}
        ON {SCHEMA}.{TABLE}
        AS RESTRICTIVE
        FOR ALL
        USING ({expression})
        WITH CHECK ({expression})
        """
    )

    # 授权：资产域角色可读、可插入（交付清单归资产域）；**不给 UPDATE/DELETE**。
    # 需要追加事件，因此同时授予 outbox 的 SELECT/INSERT（与其它域一致）。
    asset_role = role_for(DatabaseDomain.ASSET)
    op.execute(
        f"GRANT SELECT, INSERT ON {SCHEMA}.{TABLE} TO {asset_role}"
    )
    op.execute(
        f"GRANT SELECT, INSERT ON {OUTBOX_SCHEMA}.outbox_events TO {asset_role}"
    )
    op.execute(f"GRANT SELECT ON {ALEMBIC_VERSION} TO {asset_role}")


def downgrade() -> None:
    asset_role = role_for(DatabaseDomain.ASSET)
    op.execute(f"REVOKE ALL ON {SCHEMA}.{TABLE} FROM {asset_role}")
    for policy in (f"project_scope_{TABLE}", f"tenant_isolation_{TABLE}"):
        op.execute(f"DROP POLICY IF EXISTS {policy} ON {SCHEMA}.{TABLE}")
    op.execute(f"ALTER TABLE {SCHEMA}.{TABLE} DISABLE ROW LEVEL SECURITY")
    op.execute(f"DROP TRIGGER IF EXISTS trg_{TABLE}_guard ON {SCHEMA}.{TABLE}")
    op.execute(f"DROP FUNCTION IF EXISTS {SCHEMA}.{TABLE}_guard()")
    op.drop_index(
        "ix_asset_service_delivery_manifests_organization_id",
        table_name=TABLE,
        schema=SCHEMA,
    )
    op.drop_index(
        "ix_asset_service_delivery_manifests_tenant_id",
        table_name=TABLE,
        schema=SCHEMA,
    )
    op.drop_index(
        "ix_asset_service_delivery_manifests_project", table_name=TABLE, schema=SCHEMA
    )
    op.drop_table(TABLE, schema=SCHEMA)
