"""受控例外的运行前提：表权限 **与** RLS 可见性

为什么这两件事必须在一个迁移里
------------------------------
``b0c9`` 建了守卫例外，实测却发现「例外装好了却没有主体能用」，原因有两层，
而且**两层都会把错误表现成同一种现象**：

1. **没有表权限**：``afs_data_repair`` 在 ``role_table_grants`` 里零行，
   UPDATE 直接 ``42501``；
2. **RLS 先于守卫**：即便有了表权限，原有 PERMISSIVE 策略要求
   ``app.current_org_id`` / ``app.current_tenant_id``，而修复会话不该、也无法
   为「组织错配」的行设对一个能让它自己可见的上下文（行标的 org 与它真实的
   org 相矛盾，正是要修的东西）。于是 UPDATE 静默退化为 ``UPDATE 0``。

第 2 条是 ADR-R40 D-10.2.3 陷阱（b）的原文场景：**「没报错」≠「改成功」**。
只补权限不补 RLS，修复脚本会得到「0 行、无异常」，然后被误读成「无需修改」。
两者是同一职责的两半，拆开就会漏掉一半。

RLS 与守卫共用同一事实源（这是设计要点，不是实现细节）
----------------------------------------------------
守卫用「映射行存在且未盖章」决定**能不能改**；这里用**同一张映射表、同一个
未盖章条件**决定 repair 角色**能不能看见**。这样：

* 例外范围精确到**行**，不是「整表」也不是「整租户」；
* 映射行一旦盖上 ``applied_at``，可见性与可改性**同时失效**，不会有一侧残留；
* 不需要给 ``afs_data_repair`` 加 ``BYPASSRLS``——那会让爆炸半径从「工单列出的行」
  扩大到「全库所有行」，与 D-10.2.1 的最小豁免面直接冲突。

审计侧（D-10.3 要求 3/4）用 ``app.include_quarantine = 'on'`` 放行，
这是 ADR 原文指定的开关，不新造第二个。

Revision ID: d2e3f4a5b6c7
Revises: c1d2e3f4a5b7
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import NamedTuple

from alembic import op

revision: str = "d2e3f4a5b6c7"
down_revision: str | Sequence[str] | None = "c1d2e3f4a5b7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

REPAIR_ROLE = "afs_data_repair"
AUDIT_ROLE = "afs_audit"

#: 订正目标：a9b8 建立了归属外键的 14 张表（含 projects 自身）。
REPAIR_TARGETS: tuple[tuple[str, str], ...] = (
    ("project_planning", "projects"),
    ("generation_execution", "generation_tasks"),
    ("generation_execution", "shot_route_selections"),
    ("generation_execution", "production_routes"),
    ("shot_planning", "shot_drafts"),
    ("shot_planning", "shot_revisions"),
    ("shot_planning", "shot_visual_plans"),
    ("shot_planning", "constraint_packs"),
    ("asset_service", "delivery_manifests"),
    ("asset_service", "asset_versions"),
    ("asset_service", "qc_reports"),
    ("qc_governance", "gate_decisions"),
    ("qc_governance", "qc_review_sessions"),
    ("workflow", "approval_flows"),
    ("workflow", "approval_tickets"),
)

#: 审计可见性作用域：c1d2 加了隔离 RESTRICTIVE 策略的全部表。
AUDIT_TARGETS: tuple[tuple[str, str], ...] = (
    ("project_planning", "projects"),
    ("project_planning", "episodes"),
    ("project_planning", "script_revisions"),
    ("generation_execution", "generation_tasks"),
    ("generation_execution", "shot_route_selections"),
    ("shot_planning", "shot_drafts"),
    ("shot_planning", "shot_revisions"),
    ("shot_planning", "shot_specs"),
    ("shot_planning", "shot_visual_plans"),
    ("shot_planning", "constraint_packs"),
    ("asset_service", "asset_versions"),
    ("asset_service", "delivery_manifests"),
    ("asset_service", "qc_reports"),
    ("qc_governance", "gate_decisions"),
    ("qc_governance", "qc_review_sessions"),
    ("workflow", "approval_flows"),
    ("workflow", "approval_tickets"),
    ("workflow", "approval_steps"),
)

#: 审计主体**可写**的表——即 D-10.3 要求 4/6 的唯一落点。
#:
#: 为什么必须单独列出而不是「AUDIT_TARGETS 全授 UPDATE」
#: --------------------------------------------------
#: 要求 4 的 `WITH CHECK` 是「隔离项目不得开放给业务写入，**仅审计主体可写**」，
#: 要求 6 的「审计主体可见/**可写**」说的是同一件事，而隔离项目是
#: `project_planning.projects` 里的一行。所以「可写」的范围是**这一张表**，
#: 不是「审计能看见的每一张表」。
#:
#: 旧版把 UPDATE 一并授到 AUDIT_TARGETS 全部 18 张，是需求扩散，代价有三层：
#:   1. 安全：审计身份获得改动 17 张业务表的能力，与「审计只读」语义冲突；
#:   2. 功能：其中 12 张是受守卫表，而它们的函数 EXECUTE 由 b0c9 的 E6 授予，
#:      本步骤晚于 E6 执行 → 这 12 张表的 UPDATE 在守卫处必然 42501，
#:      即「权限在，功能不可用」的假可用；
#:   3. 可观测：扩散本身没有任何验收用例覆盖，靠 `verify_grants.py` 的
#:      「有表写权限 ⇒ 有函数 EXECUTE」不变量才暴露出来。
AUDIT_WRITABLE: tuple[tuple[str, str], ...] = (
    ("project_planning", "projects"),
)


class Step(NamedTuple):
    code: str
    title: str
    sql: str
    note: str = ""


#: 行标识列：多数表是 entity_id，qc_reports 是 qc_report_id。
#: RLS 策略必须知道本表用哪一列去匹配映射表的 row_key，写错一列该表就永久失效
#: 而结构检查依然全绿——所以这里显式列出，不靠命名推导。
KEY_COLS: dict[str, str] = {
    "project_planning.projects": "entity_id",
    "project_planning.episodes": "entity_id",
    "project_planning.script_revisions": "entity_id",
    "generation_execution.generation_tasks": "entity_id",
    "generation_execution.shot_route_selections": "entity_id",
    "generation_execution.production_routes": "entity_id",
    "shot_planning.shot_drafts": "entity_id",
    "shot_planning.shot_revisions": "entity_id",
    "shot_planning.shot_specs": "entity_id",
    "shot_planning.shot_visual_plans": "entity_id",
    "shot_planning.constraint_packs": "entity_id",
    "asset_service.asset_versions": "entity_id",
    "asset_service.delivery_manifests": "entity_id",
    "asset_service.qc_reports": "qc_report_id",
    "qc_governance.gate_decisions": "entity_id",
    "qc_governance.qc_review_sessions": "entity_id",
    "workflow.approval_flows": "entity_id",
    "workflow.approval_tickets": "entity_id",
    "workflow.approval_steps": "entity_id",
}

INCLUDE_GUC = "app.include_quarantine"
GUC = "app.ownership_repair_ticket"


def _all_tables() -> tuple[str, ...]:
    """REPAIR ∪ AUDIT 的并集，保持声明顺序（与台账/策略清单便于逐行对照）。"""
    seen: dict[str, None] = {}
    for schema, table in REPAIR_TARGETS + AUDIT_TARGETS:
        seen.setdefault(f"{schema}.{table}", None)
    return tuple(seen)


def upgrade_steps() -> list[Step]:
    steps: list[Step] = []

    steps.append(
        Step(
            "R0",
            "前置断言：两个角色与 14 张目标表都必须存在",
            f"""
            DO $do$
            DECLARE missing text;
            BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{REPAIR_ROLE}') THEN
                    RAISE EXCEPTION 'PREFLIGHT: 角色 % 不存在——须先执行 b0c9d8e7f6a5', '{REPAIR_ROLE}';
                END IF;
                IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{AUDIT_ROLE}') THEN
                    RAISE EXCEPTION 'PREFLIGHT: 角色 % 不存在——须先执行 c1d2e3f4a5b7', '{AUDIT_ROLE}';
                END IF;
                SELECT string_agg(fqn, ', ') INTO missing
                  FROM unnest(ARRAY[{", ".join("'" + s + "." + t + "'" for s, t in REPAIR_TARGETS)}]) fqn
                 WHERE to_regclass(fqn) IS NULL;
                IF missing IS NOT NULL THEN
                    RAISE EXCEPTION 'PREFLIGHT: 目标表不存在: %', missing;
                END IF;
            END $do$;
            """,
            "fail-closed：缺表就中止，不用 IF EXISTS 把「没授到」静默成本地成功",
        )
    )

    # schema USAGE 是表权限生效的前提，漏了它 GRANT ON TABLE 也不会生效。
    for schema in sorted({s for s, _ in REPAIR_TARGETS} | {s for s, _ in AUDIT_TARGETS}):
        steps.append(
            Step(
                "R1",
                f"schema {schema} 的 USAGE 授权",
                f"GRANT USAGE ON SCHEMA {schema} TO {REPAIR_ROLE}, {AUDIT_ROLE}",
            )
        )

    for schema, table in REPAIR_TARGETS:
        qualified = f"{schema}.{table}"
        steps.append(
            Step(
                "R2",
                f"{qualified}：repair 角色 SELECT + UPDATE（不含 INSERT/DELETE）",
                f"GRANT SELECT, UPDATE ON TABLE {qualified} TO {REPAIR_ROLE}",
                "SELECT 不可省：UPDATE ... WHERE 的 WHERE 列需要它，"
                "否则 42501 会被误读成守卫拒绝（D-10.2.3 陷阱 c）",
            )
        )

    for schema, table in AUDIT_TARGETS:
        qualified = f"{schema}.{table}"
        writable = (schema, table) in AUDIT_WRITABLE
        privs = "SELECT, UPDATE" if writable else "SELECT"
        note = (
            "D-10.3 要求 4 的 WITH CHECK 是「仅审计主体可写隔离项目」，要求 6 的"
            "「审计主体可见/可写」指同一件事，落点是**隔离项目所在的那一行**。"
            if writable
            else "审计身份的语义是「能看见」，不是「能改」。"
            "旧版把 UPDATE 一并授到全部 18 张表，是需求扩散：那 17 张表的写权限"
            "既无任何验收用例使用，又让审计身份获得改动业务数据的能力，"
            "其中 12 张受守卫表还因为 E6 早于本步骤执行而过期漏授判定函数——"
            "有表写权限却无函数 EXECUTE，真去写必然 42501，是「有钥匙没门」的假可用。"
        )
        steps.append(
            Step(
                "R3",
                f"{qualified}：审计角色 {privs}",
                f"GRANT {privs} ON TABLE {qualified} TO {AUDIT_ROLE}",
                note,
            )
        )

    steps.append(
        Step(
            "R4",
            "repair 角色对映射表只需 SELECT（判定函数以 DEFINER 读，但自查要能读）",
            f"GRANT SELECT ON TABLE project_planning.ownership_repair_map TO {REPAIR_ROLE}",
            "不授 INSERT：映射行的写入属迁移操作面，由迁移主体完成，"
            "避免「谁都能给自己开例外」",
        )
    )

    # ── R4b/R4c 两个判定函数的 EXECUTE ────────────────────────────────
    # 这两个 GRANT 填的是 b0c9 的覆盖缺口，不是重复授权。
    # b0c9 的 E5 硬编码只授 ownership_repair_allows，**没有授 map_match**；
    # 而 E6 的「按真实表授权动态收敛」只遍历**已有表写权限**的角色——
    # 当时 afs_data_repair 还没有任何表权限（那是 d2e3 才给的），于是被跳过。
    # 结果是 allows 有 EXECUTE、它内部调用的 map_match 没有：
    # 守卫一走到 `ownership_repair_map_match(...)` 就 42501。
    # 这类"顺序依赖的授权缺口"只有真正跑一次订正才会显形。
    for fn in (
        "project_planning.ownership_repair_allows(regclass,jsonb,jsonb,text)",
        "project_planning.ownership_repair_map_match(text,text,text)",
    ):
        steps.append(
            Step(
                "R4b",
                f"{REPAIR_ROLE} 对 {fn.split('(')[0]} 的 EXECUTE",
                f"GRANT EXECUTE ON FUNCTION {fn} TO {REPAIR_ROLE}",
                "幂等：重复 GRANT 无副作用；两函数都要，缺一个就在运行时炸",
            )
        )

    # ── R5/R6 RLS 可见性：与守卫共用映射表作为唯一事实源 ────────────────
    for qualified in _all_tables():
        key = KEY_COLS[qualified]
        steps.append(
            Step(
                "R5a",
                f"{qualified}：移除旧 repair 可见性策略（幂等前置）",
                f"DROP POLICY IF EXISTS repair_row_scope ON {qualified}",
            )
        )
        steps.append(
            Step(
                "R5b",
                f"{qualified}：repair 只看得见「工单列出且未盖章」的行",
                f"""
                CREATE POLICY repair_row_scope ON {qualified}
                    AS PERMISSIVE
                    FOR ALL
                    TO {REPAIR_ROLE}
                    USING (
                        current_setting('{GUC}', true) IS NOT NULL
                        AND EXISTS (
                            SELECT 1 FROM project_planning.ownership_repair_map m
                             WHERE m.table_name = '{qualified}'
                               AND m.row_key = {key}::text
                               AND m.ticket_ref = current_setting('{GUC}', true)
                               AND m.applied_at IS NULL
                        )
                    )
                    WITH CHECK (
                        current_setting('{GUC}', true) IS NOT NULL
                        AND EXISTS (
                            SELECT 1 FROM project_planning.ownership_repair_map m
                             WHERE m.table_name = '{qualified}'
                               AND m.row_key = {key}::text
                               AND m.ticket_ref = current_setting('{GUC}', true)
                               AND m.applied_at IS NULL
                        )
                    )
                """,
                "与守卫同源：同一张映射表、同一个未盖章条件。"
                "盖章后可见性与可改性同时失效，不会有一侧残留。"
                "刻意不用 BYPASSRLS：那会把爆炸半径从「工单列出的行」放大到「全库」",
            )
        )
        steps.append(
            Step(
                "R6a",
                f"{qualified}：移除旧审计可见性策略（幂等前置）",
                f"DROP POLICY IF EXISTS audit_quarantine_scope ON {qualified}",
            )
        )
        steps.append(
            Step(
                "R6b",
                f"{qualified}：审计主体在 app.include_quarantine='on' 时可见/可写",
                f"""
                CREATE POLICY audit_quarantine_scope ON {qualified}
                    AS PERMISSIVE
                    FOR ALL
                    TO {AUDIT_ROLE}
                    USING (current_setting('{INCLUDE_GUC}', true) = 'on')
                    WITH CHECK (current_setting('{INCLUDE_GUC}', true) = 'on')
                """,
                "D-10.3 要求 3 指定的开关就是 app.include_quarantine，"
                "不新造第二个开关，避免「两套审计放行语义」",
            )
        )

    return steps


def downgrade_steps() -> list[Step]:
    steps: list[Step] = []
    # 策略先删：它引用映射表与两个角色，若先撤权限，策略求值时可能 42501，
    # 让降级失败在一个与降级意图无关的错误上。
    for qualified in _all_tables():
        steps.append(
            Step("D", f"{qualified}：移除 repair 可见性策略",
                 f"DROP POLICY IF EXISTS repair_row_scope ON {qualified}")
        )
        steps.append(
            Step("D", f"{qualified}：移除审计可见性策略",
                 f"DROP POLICY IF EXISTS audit_quarantine_scope ON {qualified}")
        )
    for schema, table in REPAIR_TARGETS:
        steps.append(
            Step("D", f"撤销 {schema}.{table} 的 repair 表权限",
                 f"REVOKE SELECT, UPDATE ON TABLE {schema}.{table} FROM {REPAIR_ROLE}")
        )
    for schema, table in AUDIT_TARGETS:
        steps.append(
            Step("D", f"撤销 {schema}.{table} 的审计表权限",
                 f"REVOKE SELECT, UPDATE ON TABLE {schema}.{table} FROM {AUDIT_ROLE}",
                 "刻意比升级多撤一个 UPDATE（升级只对 AUDIT_WRITABLE 授它）："
                 "REVOKE 未授予的权限是幂等 no-op，而多撤这一下能把手工补授的残留一并清掉。"
                 "降级的目标是「回到干净基线」，不是「逐字回放升级的相反数」。")
        )
    steps.append(
        Step("D", "撤销映射表 SELECT",
             f"REVOKE SELECT ON TABLE project_planning.ownership_repair_map FROM {REPAIR_ROLE}")
    )
    for fn in (
        "project_planning.ownership_repair_allows(regclass,jsonb,jsonb,text)",
        "project_planning.ownership_repair_map_match(text,text,text)",
    ):
        steps.append(
            Step("D", f"撤销 {REPAIR_ROLE} 对 {fn.split('(')[0]} 的 EXECUTE",
                 f"REVOKE EXECUTE ON FUNCTION {fn} FROM {REPAIR_ROLE}")
        )
    for schema in sorted({s for s, _ in REPAIR_TARGETS} | {s for s, _ in AUDIT_TARGETS}):
        steps.append(
            Step("D", f"撤销 schema {schema} 的 USAGE",
                 f"REVOKE USAGE ON SCHEMA {schema} FROM {REPAIR_ROLE}, {AUDIT_ROLE}")
        )
    return steps


def upgrade() -> None:
    for step in upgrade_steps():
        op.execute(step.sql)


def downgrade() -> None:
    for step in downgrade_steps():
        op.execute(step.sql)
