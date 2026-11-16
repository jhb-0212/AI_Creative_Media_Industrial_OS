"""资产状态转移纳入数据库约束：status 只能沿已登记边推进（ADR-R20）

Revision ID: b3c4d5e6f7a8
Revises: e7f8a9b0c1d2
Create Date: 2026-11-17

背景（审查第三轮第 13 条）：
- `d1e2f3a4b5c6` 把资产内容列锁成不可变，但白名单里包含 `status`——为了让
  状态迁移能走通。结果是：**内容改不了，状态随便改**。任何拿着资产域角色的代码
  （或运维直连）都能把 GENERATED 直接写成 GOLD，跳过 QC 与人工审核。
  "状态机"当时只存在于 Python 的 `ASSET_ALLOWED_TRANSITIONS` 里，一句
  `UPDATE ... SET status = 'GOLD'` 就能绕过它。

本迁移做三件事：
1. 取值收口：给 `asset_versions.status` 加 CHECK 约束，只接受
   `contracts/enums/enum_registry.yaml` 里 AssetVersion 登记的 10 个状态。
2. 转移集合落成数据表 `asset_service.asset_status_transitions`，由迁移播种
   已登记边（对齐 `domain/state_machine.py::ASSET_ALLOWED_TRANSITIONS`）。
3. 升级 `asset_versions_guard()`：status 变化时 (OLD.status, NEW.status) 必须
   在转移表里，否则拒绝；内容列不可变的判定原样保留，且 `status` 从「可变
   白名单」里移除——它不再是"随便可改"，而是"只能沿边改"。

为什么边用表而不是写死在函数体里：
- 契约补登旁支来源边（REJECTED / REWORK / SUPERSEDED / ARCHIVED …）时只需
  插入一行数据，不必重建函数体；
- 边是**可查询的事实**：`SELECT * FROM asset_status_transitions` 就是当前
  允许的状态机，运维与审计不必读 PL/pgSQL；
- 应用角色对该表**没有任何写权限**（只 SELECT），"加一条边来自我放行"这条路
  在数据库层就被关掉。

为什么取值用 CHECK 而不是引用数据表：
- 信封列 `status` 由 `EntityMixin` 提供，被 Project / Episode / ScriptRevision
  共用；给它挂一个指向资产域参考表的外键，会把这四个域绑成一条隐式依赖。
- 取值集合来自契约枚举，变更本来就要走契约修订，"再写一条迁移"是可接受的
  代价；一致性由 `tests/unit/test_enum_parity.py` 守（比对下面两份字面量与
  当前枚举/转移表）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from packages.shared.db_roles import all_business_roles

# revision identifiers, used by Alembic.
revision: str = "b3c4d5e6f7a8"
down_revision: str | Sequence[str] | None = "e7f8a9b0c1d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "asset_service"
TRANSITIONS_TABLE = f"{SCHEMA}.asset_status_transitions"
CHECK_NAME = "ck_asset_versions_status_registered"

#: 播种用的状态取值 —— 必须与 `AssetStatus` 枚举逐值一致。
#:
#: 刻意写成字面量而不是"从代码导入"：迁移是历史快照，若它随代码变化，重放
#: 同一个 revision 会得到不同的库。一致性由 `tests/unit/test_enum_parity.py`
#: 比对这份字面量与当前枚举来守。
STATUS_VALUES: tuple[str, ...] = (
    "GENERATED",
    "TECHNICAL_QC",
    "HUMAN_REVIEW",
    "APPROVED",
    "GOLD",
    "REJECTED",
    "QUARANTINED",
    "REWORK",
    "SUPERSEDED",
    "ARCHIVED",
)

#: 播种用的状态转移边 —— 必须与 `ASSET_ALLOWED_TRANSITIONS` 完全一致。
#: 旁支状态（REJECTED 等）没有来源边：契约正文只写「可进入」，未给来源状态，
#: 按 31.5「未登记转移一律拒绝」，这里也不该出现（不可达性登记在
#: `contracts/runtime/state_machine_gaps.yaml`，由闸门双向校验）。
STATUS_TRANSITIONS: tuple[tuple[str, str], ...] = (
    ("GENERATED", "TECHNICAL_QC"),
    ("TECHNICAL_QC", "HUMAN_REVIEW"),
    ("HUMAN_REVIEW", "APPROVED"),
    ("APPROVED", "GOLD"),
)

_GUARD_ASSET_VERSIONS = """
CREATE OR REPLACE FUNCTION asset_service.asset_versions_guard()
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
            'asset_versions 是追加型不可变表，禁止删除（ADR-R11）';
    END IF;

    -- 状态转移：只允许沿 asset_status_transitions 登记过的边推进。
    -- 这是"状态机在数据库层成立"的唯一执行点：服务层判定在
    -- domain/state_machine.py，直连 SQL 的写入不经服务层，必须在这里拦。
    old_status := to_jsonb(OLD) ->> 'status';
    new_status := to_jsonb(NEW) ->> 'status';
    IF old_status IS DISTINCT FROM new_status THEN
        IF NOT EXISTS (
            SELECT 1 FROM asset_service.asset_status_transitions t
            WHERE t.from_status = old_status AND t.to_status = new_status
        ) THEN
            RAISE EXCEPTION
                '非法状态转移 % -> %（未在 asset_status_transitions 登记，ADR-R20）',
                old_status, new_status;
        END IF;
    END IF;

    -- 逐列扫描当前表的真实列，新增内容列自动纳入保护，不依赖手写清单。
    -- status 仍在此跳过，但**不再等于"随便可改"**：它的每一次变化都要过上面
    -- 的转移表判定。若把它留在这里当内容是另一种错——合法转移会被"不可变
    -- 内容列"挡住，状态机永远走不动。
    FOR col IN
        SELECT attname
        FROM pg_attribute
        WHERE attrelid = 'asset_service.asset_versions'::regclass
          AND attnum > 0
          AND NOT attisdropped
    LOOP
        IF col NOT IN ('status', 'entity_version', 'updated_at', 'audit_ref') THEN
            IF (to_jsonb(OLD) ->> col) IS DISTINCT FROM (to_jsonb(NEW) ->> col) THEN
                RAISE EXCEPTION
                    'asset_versions.% 为不可变内容列，禁止改写（ADR-R11）', col;
            END IF;
        END IF;
    END LOOP;

    RETURN NEW;
END;
$$;
"""

#: 回滚用的旧版守卫函数（= d1e2f3a4b5c6 的实现：status 在可变白名单内）。
_LEGACY_GUARD = """
CREATE OR REPLACE FUNCTION asset_service.asset_versions_guard()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    col text;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION
            'asset_versions 是追加型不可变表，禁止删除（ADR-R11）';
    END IF;

    FOR col IN
        SELECT attname
        FROM pg_attribute
        WHERE attrelid = 'asset_service.asset_versions'::regclass
          AND attnum > 0
          AND NOT attisdropped
    LOOP
        IF col NOT IN ('status', 'entity_version', 'updated_at', 'audit_ref') THEN
            IF (to_jsonb(OLD) ->> col) IS DISTINCT FROM (to_jsonb(NEW) ->> col) THEN
                RAISE EXCEPTION
                    'asset_versions.% 为不可变内容列，禁止改写（ADR-R11）', col;
            END IF;
        END IF;
    END LOOP;

    RETURN NEW;
END;
$$;
"""


def _create_transitions_table() -> None:
    """建转移表并播种已登记边。"""
    op.create_table(
        "asset_status_transitions",
        sa.Column("from_status", sa.String(32), primary_key=True, nullable=False),
        sa.Column("to_status", sa.String(32), primary_key=True, nullable=False),
        schema=SCHEMA,
    )
    transitions = sa.table(
        "asset_status_transitions",
        sa.column("from_status", sa.String),
        sa.column("to_status", sa.String),
        schema=SCHEMA,
    )
    op.bulk_insert(
        transitions,
        [{"from_status": src, "to_status": tgt} for src, tgt in STATUS_TRANSITIONS],
    )


def _add_status_check() -> None:
    """status 只能取契约登记的状态值。"""
    values = ", ".join(f"'{v}'" for v in STATUS_VALUES)
    op.execute(
        f"ALTER TABLE {SCHEMA}.asset_versions "
        f"ADD CONSTRAINT {CHECK_NAME} CHECK (status IN ({values}))"
    )


def _grant_read_only() -> None:
    """转移表对所有业务角色只读。

    触发器函数默认 SECURITY INVOKER：执行 UPDATE 的角色需要能 SELECT 这张表，
    否则守卫会以 ``permission denied`` 失败——那会把"非法转移"和"没权限读参考表"
    混成同一种错误，排障方向完全错。同时**不授予任何写权限**：能改转移表的人
    就能给自己放行任意状态跳转。
    """
    for role in all_business_roles():
        op.execute(f"GRANT SELECT ON {TRANSITIONS_TABLE} TO {role}")


def upgrade() -> None:
    _create_transitions_table()
    _add_status_check()
    # 顺序是硬要求：函数体引用转移表，表必须先存在（plpgsql 不在创建时校验
    # SQL，但缺表会让第一次 UPDATE 以 "relation does not exist" 失败）。
    op.execute(_GUARD_ASSET_VERSIONS)
    _grant_read_only()


def downgrade() -> None:
    # 函数先回到 d1e2f3a4b5c6 的形态（status 重新进入可变白名单），再拆表与约束。
    op.execute(_LEGACY_GUARD)
    op.execute(
        f"ALTER TABLE {SCHEMA}.asset_versions DROP CONSTRAINT IF EXISTS {CHECK_NAME}"
    )
    for role in all_business_roles():
        op.execute(f"REVOKE ALL ON {TRANSITIONS_TABLE} FROM {role}")
    op.drop_table("asset_status_transitions", schema=SCHEMA)
