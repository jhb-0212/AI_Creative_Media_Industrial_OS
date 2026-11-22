"""受控例外的第三块拼图：跨组织订正必须旁路 project_scope（RESTRICTIVE）

为什么非改既有策略不可（这是推导结论，不是偏好）
------------------------------------------------
分支的 RLS 设计是两类策略的组合：

* ``tenant_isolation_*``  —— **PERMISSIVE**，只要求 ``app.current_tenant_id``；
* ``project_scope_*`` / ``organization_scope_*`` —— **RESTRICTIVE**，
  要求 ``organization_id = app.current_org_id`` **且** ``project_id`` 在白名单内。

RESTRICTIVE 与其他策略是 **AND** 关系。于是「把某行的 ``organization_id``
从错误的 org-2 改成正确的 org-1」这件事，在同一会话里**无解**：

* 若 ``app.current_org_id = org-2`` → USING 看得见（行当前就是 org-2），
  但 WITH CHECK 要求新行仍是 org-2 → **新值 org-1 被拒**；
* 若 ``app.current_org_id = org-1`` → WITH CHECK 通过，USING 看不见该行 → **UPDATE 0**。

两条路都走不通，不是参数没调对。要让「组织错配」这一类（恰恰是最需要修的一类）
可被修复，就必须给 ``afs_data_repair`` 一条旁路。这不是放宽业务侧，
而是承认**修复角色的可见性需求与业务主体本质不同**：
业务主体只需要看自己组织的数据；修复角色必须同时看见「行现在标的」与「它应属的」。

为什么不用 ``BYPASSRLS``（更省事但不对）
--------------------------------------
``ALTER ROLE afs_data_repair BYPASSRLS`` 一行就能通。但那样一来，
例外范围从「工单列出的那几行」放大成「全库所有行、所有租户」，
与 D-10.2.1 要求的**最小豁免面**直接冲突，也让「RLS 与守卫共用映射表」
这个设计要点落空。所以选择改策略，且新分支的判定条件**仍然是那张映射表**。

为什么判定函数必须是 PL/pgSQL 且先判角色
----------------------------------------
策略表达式对**所有**访问该表的角色求值，包括普通业务角色。若写成 SQL 函数，
``AND`` 不保证从左到右短路——一旦先执行了映射表子查询，没有 map 表 SELECT
权限的业务角色会拿到 ``42501`` 而不是「看不见」。用 PL/pgSQL 的显式
``IF ... RETURN false`` 才有确定的求值顺序。

另：**不得**加 ``SECURITY DEFINER``。那会让 ``current_user`` 变成函数 owner，
``pg_has_role`` 恒真，角色条件静默失效——这正是 D-10.2.3 陷阱（a），
``b0c9`` 已经踩过一次。

修订性质（如实登记）
--------------------
本迁移**修改了分支既有的 19 条策略**。原条件逐字保留，只在末尾追加一个 OR 分支。
这是本轮唯一对既有 RLS 设计的侵入性修改，理由如上；不接受「把它当成
业务策略的一部分」来淡化——它是修复通道的专用旁路，只对 ``afs_data_repair`` 生效。

Revision ID: e3f4a5b6c7d8
Revises: d2e3f4a5b6c7
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import NamedTuple

from alembic import op

revision: str = "e3f4a5b6c7d8"
down_revision: str | Sequence[str] | None = "d2e3f4a5b6c7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FUNC = "project_planning.repair_row_allowed"
REPAIR_ROLE = "afs_data_repair"

BACKUP = "project_planning.rls_policy_backup"

#: (schema, table, 策略名, 行键列)
#: 策略名不按 ``project_scope_<table>`` 推导——``production_routes`` 用的是
#: ``organization_scope_production_routes``，按命名推导会漏掉它。
SCOPED_POLICIES: tuple[tuple[str, str, str, str], ...] = (
    ("project_planning", "projects", "project_scope_projects", "entity_id"),
    ("project_planning", "episodes", "project_scope_episodes", "entity_id"),
    ("project_planning", "script_revisions", "project_scope_script_revisions", "entity_id"),
    ("generation_execution", "generation_tasks", "project_scope_generation_tasks", "entity_id"),
    ("generation_execution", "shot_route_selections", "project_scope_shot_route_selections", "entity_id"),
    ("generation_execution", "production_routes", "organization_scope_production_routes", "entity_id"),
    ("shot_planning", "shot_drafts", "project_scope_shot_drafts", "entity_id"),
    ("shot_planning", "shot_revisions", "project_scope_shot_revisions", "entity_id"),
    ("shot_planning", "shot_specs", "project_scope_shot_specs", "entity_id"),
    ("shot_planning", "shot_visual_plans", "project_scope_shot_visual_plans", "entity_id"),
    ("shot_planning", "constraint_packs", "project_scope_constraint_packs", "entity_id"),
    ("asset_service", "asset_versions", "project_scope_asset_versions", "entity_id"),
    ("asset_service", "delivery_manifests", "project_scope_delivery_manifests", "entity_id"),
    ("asset_service", "qc_reports", "project_scope_qc_reports", "qc_report_id"),
    ("qc_governance", "gate_decisions", "project_scope_gate_decisions", "entity_id"),
    ("qc_governance", "qc_review_sessions", "project_scope_qc_review_sessions", "entity_id"),
    ("workflow", "approval_flows", "project_scope_approval_flows", "entity_id"),
    ("workflow", "approval_tickets", "project_scope_approval_tickets", "entity_id"),
    ("workflow", "approval_steps", "project_scope_approval_steps", "entity_id"),
)


class Step(NamedTuple):
    code: str
    title: str
    sql: str
    note: str = ""


def upgrade_steps() -> list[Step]:
    steps: list[Step] = []

    steps.append(
        Step(
            "S0",
            "前置断言：映射表、判定函数依赖的角色都必须就位",
            f"""
            DO $do$ BEGIN
                IF to_regclass('project_planning.ownership_repair_map') IS NULL THEN
                    RAISE EXCEPTION 'PREFLIGHT: 映射表不存在——须先执行 b0c9d8e7f6a5';
                END IF;
                IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{REPAIR_ROLE}') THEN
                    RAISE EXCEPTION 'PREFLIGHT: 角色 % 不存在', '{REPAIR_ROLE}';
                END IF;
            END $do$;
            """,
        )
    )

    steps.append(
        Step(
            "S1",
            f"备份表 {BACKUP}：保存被改写策略的**原表达式**",
            f"""
            CREATE TABLE IF NOT EXISTS {BACKUP} (
                schema_name text        NOT NULL,
                table_name  text        NOT NULL,
                policy_name text        NOT NULL,
                qual        text        NOT NULL,
                with_check  text,
                saved_at    timestamptz NOT NULL DEFAULT now(),
                CONSTRAINT rls_policy_backup_pkey
                    PRIMARY KEY (schema_name, table_name, policy_name)
            )
            """,
            "降级必须能**逐字**还原原表达式。第一版试图用正则剥掉追加的 OR 分支，"
            "但 PostgreSQL 会把 entity_id::text 规范化成 (entity_id)::text，"
            "且整体多包一层括号——字符串正则做不了括号平衡，还原必然失败。"
            "备份原值是这个问题的唯一可靠解，且它本身就是可审计的还原点",
        )
    )

    steps.append(
        Step(
            "S2",
            f"判定函数 {FUNC}(table, row_key)：INVOKER + 显式短路",
            f"""
            CREATE OR REPLACE FUNCTION {FUNC}(p_table text, p_row_key text)
            RETURNS boolean
            LANGUAGE plpgsql
            STABLE
            AS $fn$
            BEGIN
                -- 必须**先**判角色再碰映射表：策略对所有角色求值，
                -- 业务角色没有 map 表 SELECT 权限，先查表会拿到 42501 而不是「看不见」。
                -- 用 plpgsql 而非 sql，是为了让求值顺序确定，不依赖 AND 的短路实现。
                IF NOT pg_has_role(current_user, '{REPAIR_ROLE}', 'member') THEN
                    RETURN false;
                END IF;
                IF NULLIF(current_setting('app.ownership_repair_ticket', true), '') IS NULL THEN
                    RETURN false;
                END IF;
                RETURN EXISTS (
                    SELECT 1 FROM project_planning.ownership_repair_map m
                     WHERE m.table_name = p_table
                       AND m.row_key = p_row_key
                       AND m.ticket_ref = current_setting('app.ownership_repair_ticket', true)
                       AND m.applied_at IS NULL
                );
            END;
            $fn$;
            """,
            "不加 SECURITY DEFINER：那会让 current_user 变成 owner、pg_has_role 恒真，"
            "角色条件静默失效（D-10.2.3 陷阱 a，b0c9 踩过一次）",
        )
    )

    steps.append(
        Step(
            "S3",
            "函数注释：说明这是修复通道专用旁路，不是业务策略",
            f"""
            COMMENT ON FUNCTION {FUNC}(text, text)
                IS '受控例外的 RLS 旁路判定：仅 afs_data_repair + 工单令牌 + 映射行未盖章时返回 true。'
                   '策略对其余角色应表现为 false，不得据此放宽业务可见性。'
            """,
        )
    )

    for schema, table, policy, key in SCOPED_POLICIES:
        qualified = f"{schema}.{table}"
        # 用 pg_policies 现读原表达式，逐字保留，只在末尾追加 OR 分支。
        # 不在这里重抄一遍原条件——重抄就会与既有迁移漂移，而漂移是无提示的。
        steps.append(
            Step(
                "S4",
                f"{qualified}.{policy}：备份原表达式后追加 repair 旁路分支",
                f"""
                DO $do$
                DECLARE q text; c text;
                BEGIN
                    SELECT qual, with_check INTO q, c
                      FROM pg_policies
                     WHERE schemaname = '{schema}'
                       AND tablename = '{table}'
                       AND policyname = '{policy}';
                    IF q IS NULL THEN
                        RAISE EXCEPTION
                            'PREFLIGHT: 策略 %.% 不存在——谱系不符或已被改名',
                            '{table}', '{policy}';
                    END IF;

                    IF position('repair_row_allowed' in q) > 0 THEN
                        RETURN;   -- 已打过补丁，幂等
                    END IF;

                    -- 先备份原值再改：备份失败就不改，避免"改了但还原不了"。
                    INSERT INTO {BACKUP} (schema_name, table_name, policy_name, qual, with_check)
                    VALUES ('{schema}', '{table}', '{policy}', q, c)
                    ON CONFLICT ON CONSTRAINT rls_policy_backup_pkey DO NOTHING;

                    EXECUTE format(
                        'ALTER POLICY {policy} ON {qualified} '
                        'USING ((%s) OR {FUNC}(%L, {key}::text))',
                        q, '{qualified}'
                    );
                    IF c IS NOT NULL THEN
                        EXECUTE format(
                            'ALTER POLICY {policy} ON {qualified} '
                            'WITH CHECK ((%s) OR {FUNC}(%L, {key}::text))',
                            c, '{qualified}'
                        );
                    END IF;
                END $do$;
                """,
                "原条件由 pg_policies 现读现用并存档，避免与既有迁移产生第二份定义；"
                "「先备份、后修改」的顺序不可颠倒",
            )
        )

    return steps


def downgrade_steps() -> list[Step]:
    steps: list[Step] = []
    for schema, table, policy, _key in SCOPED_POLICIES:
        qualified = f"{schema}.{table}"
        steps.append(
            Step(
                "D",
                f"{qualified}.{policy}：从备份逐字还原原表达式",
                f"""
                DO $do$
                DECLARE bq text; bc text;
                BEGIN
                    SELECT qual, with_check INTO bq, bc
                      FROM {BACKUP}
                     WHERE schema_name = '{schema}'
                       AND table_name = '{table}'
                       AND policy_name = '{policy}';

                    IF bq IS NULL THEN
                        -- 没有备份：要么从未打过补丁（跳过），要么备份表已被误删。
                        -- 后者必须报错——不能因为"找不到原值"就假装还原成功。
                        IF EXISTS (
                            SELECT 1 FROM pg_policies
                             WHERE schemaname = '{schema}'
                               AND tablename = '{table}'
                               AND policyname = '{policy}'
                               AND position('repair_row_allowed' in qual) > 0
                        ) THEN
                            RAISE EXCEPTION
                                '降级失败：% 仍含旁路分支但备份缺失，拒绝留下半改状态',
                                '{policy}';
                        END IF;
                        RETURN;
                    END IF;

                    EXECUTE format('ALTER POLICY {policy} ON {qualified} USING (%s)', bq);
                    IF bc IS NOT NULL THEN
                        EXECUTE format('ALTER POLICY {policy} ON {qualified} WITH CHECK (%s)', bc);
                    END IF;

                    DELETE FROM {BACKUP}
                     WHERE schema_name = '{schema}'
                       AND table_name = '{table}'
                       AND policy_name = '{policy}';
                END $do$;
                """,
                "从备份逐字还原，不再对表达式做字符串手术",
            )
        )

    steps.append(
        Step(
            "D",
            "断言：不应残留任何含旁路分支的策略",
            """
            DO $do$ DECLARE n integer; BEGIN
                SELECT count(*) INTO n FROM pg_policies
                 WHERE qual LIKE '%repair_row_allowed%'
                    OR coalesce(with_check, '') LIKE '%repair_row_allowed%';
                IF n > 0 THEN
                    RAISE EXCEPTION '降级后仍有 % 条策略引用 repair_row_allowed', n;
                END IF;
            END $do$;
            """,
            "还原后必须自证干净；这一步失败说明降级逻辑与实际情况不符",
        )
    )

    steps.append(
        Step(
            "D",
            f"删除判定函数 {FUNC}",
            f"DROP FUNCTION IF EXISTS {FUNC}(text, text)",
            "必须在所有策略还原之后删：策略仍引用它时 DROP 会失败（这也是想要的行为）",
        )
    )

    steps.append(
        Step(
            "D",
            f"删除备份表 {BACKUP}",
            f"DROP TABLE IF EXISTS {BACKUP}",
            "备份只在本次迁移的生命周期内有意义，降级完成即无保留价值",
        )
    )
    return steps


def upgrade() -> None:
    for step in upgrade_steps():
        op.execute(step.sql)


def downgrade() -> None:
    for step in downgrade_steps():
        op.execute(step.sql)
