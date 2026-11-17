"""交付清单状态转移纳入数据库约束（D4 / ADR-R24）。

根因（报告 F4 + 重构包 D 尾部）：
- D1 把 delivery_manifests 做成 append-only（禁止一切 UPDATE），清单只能停在
  DRAFT，这是刻意的"起点"实现；但契约状态机要求
  DRAFT → VALIDATING → VALIDATED → RELEASED，APPROVE/RELEASE 两个动作因此
  没有落点。
- 若简单地给 asset 角色开表级 UPDATE，就会把 D1 辛苦守住的"内容不可变"
  一起打开：一条 `UPDATE ... SET asset_refs=..., status='VALIDATED'` 就能
  同时篡改交付内容并自我放行——正是资产状态机在 C1 修掉的那类洞。

本迁移复用 C1 的成熟模式，把清单拆成两类列：
1. 内容列（asset_refs / checksum / spec_ref / picture_lock_ref /
   qc_final_ref / rights_evidence_refs / rebuild_recipe / project_id 等）永远
   不可变——"每个 Manifest 版本不可变"在数据库层成立；
2. 状态列 status 只能沿 `delivery_manifest_status_transitions` 登记过的边推进，
   `entity_version` / `updated_at` / `audit_ref` 是状态推进的附加投影。

为什么消费端能可信：
- 状态推进只由 `DeliveryApprovalService` 在核验 outbox 中
  APPROVAL_TICKET_COMPLETED 权威事件后发起；客户端无法声明结论；
- 转移表对应用角色只读，"加一条边自我放行"这条路在数据库层被关掉；
- append-only 触发器对超级用户生效，兜住运维直连。

Revision ID: a2b3c4d5e6f7
Revises: f0a1b2c3d4e5
Create Date: 2026-11-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from packages.shared.db_roles import DatabaseDomain, all_business_roles, role_for

revision: str = "a2b3c4d5e6f7"
down_revision: str | Sequence[str] | None = "f0a1b2c3d4e5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "asset_service"
TABLE = "delivery_manifests"
TRANSITIONS_TABLE = f"{SCHEMA}.delivery_manifest_status_transitions"

#: 播种用的状态取值 —— 必须与 DeliveryManifestStatus 枚举逐值一致（字面量快照）。
#: 迁移是历史快照，不 import 代码；一致性由 contract_check 的枚举探针守。
STATUS_VALUES: tuple[str, ...] = (
    "DRAFT",
    "VALIDATING",
    "VALIDATED",
    "RELEASED",
    "INVALID",
    "STALE",
    "REVOKED",
    "SUPERSEDED",
)

#: 有明确来源边的转移。异常/失效态（INVALID/STALE/REVOKED/SUPERSEDED）契约只写
#: 「可进入」未给来源边，按 31.5 不造边；不可达登记在 state_machine_gaps.yaml。
STATUS_TRANSITIONS: tuple[tuple[str, str], ...] = (
    ("DRAFT", "VALIDATING"),
    ("VALIDATING", "VALIDATED"),
    ("VALIDATED", "RELEASED"),
)

#: 状态推进时允许变化的列。除这四列外所有列（含全部业务内容列）不可变。
_MUTABLE_COLUMNS = ("status", "entity_version", "updated_at", "audit_ref")

# 新守卫：DELETE 仍禁止；UPDATE 时内容列不可变 + status 变化必须命中转移表。
_GUARD = f"""
CREATE OR REPLACE FUNCTION {SCHEMA}.{TABLE}_guard()
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
            '{TABLE} 是 append-only 交付清单，禁止删除（每个 Manifest 版本不可变，'
            '修正须新建 manifest_id）';
    END IF;

    old_status := to_jsonb(OLD) ->> 'status';
    new_status := to_jsonb(NEW) ->> 'status';
    IF old_status IS DISTINCT FROM new_status THEN
        IF NOT EXISTS (
            SELECT 1 FROM {TRANSITIONS_TABLE} t
            WHERE t.from_status = old_status AND t.to_status = new_status
        ) THEN
            RAISE EXCEPTION
                '非法状态转移 % -> %（未在 delivery_manifest_status_transitions '
                '登记，ADR-R24）', old_status, new_status;
        END IF;
    END IF;

    FOR col IN
        SELECT attname
        FROM pg_attribute
        WHERE attrelid = '{SCHEMA}.{TABLE}'::regclass
          AND attnum > 0
          AND NOT attisdropped
    LOOP
        IF col NOT IN ('status', 'entity_version', 'updated_at', 'audit_ref') THEN
            IF (to_jsonb(OLD) ->> col) IS DISTINCT FROM (to_jsonb(NEW) ->> col) THEN
                RAISE EXCEPTION
                    '{TABLE}.% 为不可变内容列，禁止改写（每个 Manifest 版本不可变）',
                    col;
            END IF;
        END IF;
    END LOOP;

    RETURN NEW;
END;
$$;
"""

# 回滚守卫：D1 原版（禁止一切 UPDATE/DELETE）。
_LEGACY_GUARD = f"""
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


def upgrade() -> None:
    # 1. 转移表：应用角色只读，不能自我加边。
    op.create_table(
        "delivery_manifest_status_transitions",
        sa.Column("from_status", sa.String(32), nullable=False, primary_key=True),
        sa.Column("to_status", sa.String(32), nullable=False, primary_key=True),
        schema=SCHEMA,
    )
    for source, target in STATUS_TRANSITIONS:
        op.execute(
            sa.text(
                f"INSERT INTO {TRANSITIONS_TABLE} (from_status, to_status) "
                "VALUES (:source, :target)"
            ).bindparams(source=source, target=target)
        )
    for role in all_business_roles():
        op.execute(f"GRANT SELECT ON {TRANSITIONS_TABLE} TO {role}")

    # 2. 守卫升级：内容不可变 + status 沿边推进。
    op.execute(_GUARD)

    # 3. 列级 UPDATE 授权：只放开状态投影四列，其余内容列在授权层也改不了。
    asset_role = role_for(DatabaseDomain.ASSET)
    columns = ", ".join(_MUTABLE_COLUMNS)
    op.execute(
        f"GRANT UPDATE ({columns}) ON {SCHEMA}.{TABLE} TO {asset_role}"
    )


def downgrade() -> None:
    asset_role = role_for(DatabaseDomain.ASSET)
    op.execute(f"REVOKE UPDATE ON {SCHEMA}.{TABLE} FROM {asset_role}")
    op.execute(_LEGACY_GUARD)
    for role in all_business_roles():
        op.execute(f"REVOKE ALL ON {TRANSITIONS_TABLE} FROM {role}")
    op.drop_table("delivery_manifest_status_transitions", schema=SCHEMA)
