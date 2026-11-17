"""生成执行域 / 质量治理域：证据约束与追加式约束下沉数据库

Revision ID: c4e5f6a7b8d9
Revises: a9f1c2e3d4b5
Create Date: 2026-11-17

背景（再次审查 P2-5 / P2-7 / P3-1）：三条不变量此前只落在应用层，直连 SQL
可以绕过——而这三条恰好都是"绕过之后无法追溯"的类型。

1. **production_routes：VERIFIED 必须有证据**（16.2）。领域层已校验
   ``capability_refs`` 非空，数据库没有。运维播种走直连 SQL 就能造出
   "无证据的已验证路线"，而选路只认 VERIFIED——一条没有证据的路线会被
   当成可用能力。
2. **qc_review_sessions.decisions：只允许追加**（21.1 / C.7）。领域层
   ``record_decisions`` 只追加，但该列在守卫里是"可变列"，直连 SQL 可以整体
   改写或截断——"谁在何时判了什么"的追溯链就断了，而 Gate 判定依赖它。
   本次把守卫函数改成**前缀超集校验**：新数组的前 N 项必须与旧数组逐项相等。
   ``reviewers`` 同时收紧为"只增不减"（集合包含）。
3. **TRUNCATE 守卫**：``shot_route_selections`` 与 ``qc_review_sessions``
   只挂了 ``BEFORE UPDATE OR DELETE`` 行级触发器，``TRUNCATE`` 是语句级操作、
   不触发行触发器——迁移角色/超户一条 TRUNCATE 就能清空（应用角色无该权限，
   风险有限，但"有限"不等于"没有"）。

为什么全部下沉而不是只加应用层断言：数据库约束与业务代码的失效方式不同。
代码失效会随部署立即暴露（测试红），约束失效只会在有人绕过代码时暴露——
那时已经是事故。
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c4e5f6a7b8d9"
down_revision: str | Sequence[str] | None = "a9f1c2e3d4b5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

GEN_SCHEMA = "generation_execution"
ROUTE_TABLE = "production_routes"
SELECTION_TABLE = "shot_route_selections"
QC_SCHEMA = "qc_governance"
QC_TABLE = "qc_review_sessions"
TRANSITIONS = "qc_review_session_status_transitions"

VERIFIED_EVIDENCE_CHECK = (
    "ck_generation_route_verified_requires_evidence"
)

#: 守卫里可变列（与迁移 f1a2b3c4d5e6 同口径；改这里必须同步领域层）。
_MUTABLE_COLUMNS: tuple[str, ...] = ("decisions", "reviewers")


def _create_verified_evidence_check() -> None:
    """VERIFIED ⇒ capability_refs 非空（16.2 证据背书）。"""
    op.create_check_constraint(
        VERIFIED_EVIDENCE_CHECK,
        ROUTE_TABLE,
        "status <> 'VERIFIED' OR jsonb_array_length(capability_refs) > 0",
        schema=GEN_SCHEMA,
    )


def _replace_qc_guard() -> None:
    """重写 QC 会话守卫：在原有状态机/不可变列之上加"追加式"校验。"""
    mutable = ", ".join(repr(c) for c in _MUTABLE_COLUMNS)
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {QC_SCHEMA}.{QC_TABLE}_guard()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            col text;
            old_status text;
            new_status text;
            idx integer;
            old_len integer;
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION
                    '{QC_SCHEMA}.{QC_TABLE} 是 QC 复查证据，禁止物理删除'
                    '（结论与缺陷是 Gate 追溯的依据）';
            END IF;

            old_status := to_jsonb(OLD) ->> 'status';
            new_status := to_jsonb(NEW) ->> 'status';
            IF old_status IS DISTINCT FROM new_status THEN
                IF NOT EXISTS (
                    SELECT 1 FROM {QC_SCHEMA}.{TRANSITIONS} t
                    WHERE t.from_status = old_status AND t.to_status = new_status
                ) THEN
                    RAISE EXCEPTION
                        '非法状态转移 % -> %（未登记，31.5 / ADR-R20）',
                        old_status, new_status;
                END IF;
            END IF;

            FOR col IN
                SELECT attname
                FROM pg_attribute
                WHERE attrelid = '{QC_SCHEMA}.{QC_TABLE}'::regclass
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
                        '{QC_SCHEMA}.{QC_TABLE}.% 为不可变内容列，禁止改写', col;
                END IF;
            END LOOP;

            -- decisions 只允许追加（21.1）：新数组是旧数组的前缀超集。
            -- 逐项比较而不是只比长度：整体改写会保持长度不变，只比长度等于
            -- 没检查。改判走"再追加一条"，历史因此永远可追溯。
            old_len := jsonb_array_length(OLD.decisions);
            IF jsonb_array_length(NEW.decisions) < old_len THEN
                RAISE EXCEPTION
                    '{QC_SCHEMA}.{QC_TABLE}.decisions 只允许追加，不允许删除'
                    '（共 % 条结论被缩短）', old_len;
            END IF;
            FOR idx IN 0 .. old_len - 1 LOOP
                IF (NEW.decisions -> idx) IS DISTINCT FROM (OLD.decisions -> idx) THEN
                    RAISE EXCEPTION
                        '{QC_SCHEMA}.{QC_TABLE}.decisions[%] 不允许改写'
                        '（结论是 append-only 事实，改判须追加新结论）', idx;
                END IF;
            END LOOP;

            -- reviewers 只增不减：复核参与人是事实，不能被后来的写入抹掉。
            IF NOT (NEW.reviewers @> OLD.reviewers) THEN
                RAISE EXCEPTION
                    '{QC_SCHEMA}.{QC_TABLE}.reviewers 只允许追加参与人，不允许移除';
            END IF;

            RETURN NEW;
        END;
        $$
        """
    )


def _create_truncate_guard(schema: str, table: str) -> None:
    """语句级 TRUNCATE 守卫（行级触发器拦不住 TRUNCATE）。"""
    op.execute(
        f"""
        CREATE FUNCTION {schema}.{table}_truncate_guard()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION
                '{schema}.{table} 禁止 TRUNCATE（业务对象不可整体清空）';
        END;
        $$
        """
    )
    op.execute(
        f"CREATE TRIGGER {table}_truncate_guard_trigger "
        f"BEFORE TRUNCATE ON {schema}.{table} "
        f"FOR EACH STATEMENT EXECUTE FUNCTION {schema}.{table}_truncate_guard()"
    )


def upgrade() -> None:
    _create_verified_evidence_check()
    _replace_qc_guard()
    _create_truncate_guard(GEN_SCHEMA, SELECTION_TABLE)
    _create_truncate_guard(QC_SCHEMA, QC_TABLE)


def downgrade() -> None:
    for schema, table in ((GEN_SCHEMA, SELECTION_TABLE), (QC_SCHEMA, QC_TABLE)):
        op.execute(
            f"DROP TRIGGER {table}_truncate_guard_trigger ON {schema}.{table}"
        )
        op.execute(f"DROP FUNCTION {schema}.{table}_truncate_guard()")
    # 还原 f1a2b3c4d5e6 的守卫（无追加式校验版本）。
    mutable = ", ".join(repr(c) for c in _MUTABLE_COLUMNS)
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {QC_SCHEMA}.{QC_TABLE}_guard()
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
                    '{QC_SCHEMA}.{QC_TABLE} 是 QC 复查证据，禁止物理删除'
                    '（结论与缺陷是 Gate 追溯的依据）';
            END IF;
            old_status := to_jsonb(OLD) ->> 'status';
            new_status := to_jsonb(NEW) ->> 'status';
            IF old_status IS DISTINCT FROM new_status THEN
                IF NOT EXISTS (
                    SELECT 1 FROM {QC_SCHEMA}.{TRANSITIONS} t
                    WHERE t.from_status = old_status AND t.to_status = new_status
                ) THEN
                    RAISE EXCEPTION
                        '非法状态转移 % -> %（未登记，31.5 / ADR-R20）',
                        old_status, new_status;
                END IF;
            END IF;
            FOR col IN
                SELECT attname
                FROM pg_attribute
                WHERE attrelid = '{QC_SCHEMA}.{QC_TABLE}'::regclass
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
                        '{QC_SCHEMA}.{QC_TABLE}.% 为不可变内容列，禁止改写', col;
                END IF;
            END LOOP;
            RETURN NEW;
        END;
        $$
        """
    )
    op.drop_constraint(
        VERIFIED_EVIDENCE_CHECK, ROUTE_TABLE, type_="check", schema=GEN_SCHEMA
    )
