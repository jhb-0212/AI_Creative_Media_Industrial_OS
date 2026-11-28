"""隔离项目与 RLS：D-10.3 七条要求的数据库侧落地

设计依据是 ADR-R40 v1.4.6 §13 **D-10.3**（「哨兵行默认移入隔离项目」），
不是本文件的作者发挥。逐条对应关系写在每个 Step 的 ``note`` 里。

为什么隔离项目必须是真实的 ``projects`` 行
------------------------------------------
D-10.3 要求 1 明确「必须是真实存在、符合归属模型的项目记录，不能只是伪造 UUID」。
理由不是审美：哨兵行的归属要靠**三列外键** ``(project_id, tenant_id, organization_id)``
指向 ``projects`` 才能 ``VALIDATE``。伪造一个只在映射表里登记的 UUID，外键永远
``23503``，``VALIDATE`` 永远是 0/14。隔离项目是让闭环可达的**唯一默认路径**。

为什么用 ``ARCHIVED`` 而不是新状态
----------------------------------
``policies``（要求 2）要求「终态、不回流、禁止被状态机推进回 ACTIVE」。
``ProjectStatus`` 的 ``ARCHIVED`` 本就是终态（``domain/state_machine.py``，
且「终态不可原地复活」是项目红线），选它等于**复用已有的终态约束**，
而不是新造一个需要额外守卫的枚举值。另加 CHECK 作为数据库层双保险。

一处与 ADR 文字的实现偏差（明确登记，不静默）
---------------------------------------------
D-10.3 要求 2 写「``project_tier`` 取专用值」。但冻结契约
``contracts/schemas/object_schemas.schema.json`` 的 ``project_tier`` 枚举只有
``S/A/B``，``services/project_planning/domain/enums.py::ProjectTier`` 同样只有三个值。
写入 ``'Q'`` 之类的新值会让**项目读取接口在反序列化阶段抛错**——即为了标记隔离
而让隔离项目在 API 层不可读。且契约已冻结哈希（改动须走基线修订流程）。

故本轮：``project_tier`` 取枚举内合法值 ``'B'``，隔离语义由
**``entity_id`` 前缀 + ``status='ARCHIVED'`` + ``project_name`` 三个可查事实**共同表达，
判定逻辑全部收敛到 ``project_planning.is_quarantine_project(text)`` 一个函数。
该偏差已登记于 ``docs/audits/2026-11-20-quarantine-project-activation.md``，
需在 ADR 下一版澄清「专用值」是指新增枚举值，还是指可识别的固定组合。

Revision ID: c1d2e3f4a5b7
Revises: b0c9d8e7f6a5
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import NamedTuple

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c1d2e3f4a5b7"
down_revision: str | Sequence[str] | None = "b0c9d8e7f6a5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PLANNING = "project_planning"
PROJECTS = f"{PLANNING}.projects"
ORGS = f"{PLANNING}.organizations"

QUARANTINE_PREFIX = "quarantine-"
AUDIT_ROLE = "afs_audit"
REPAIR_ROLE = "afs_data_repair"
INCLUDE_GUC = "app.include_quarantine"

#: ``(schema, table)`` —— 全部带 ``project_id`` 的域表。
#: 与 a9b8 的 PROJECT_LEVEL 不是同一集合，刻意不合并：
#:   - 这里是「RLS 可见性」的作用域（按列存在性判定）；
#:   - 那里是「归属外键」的作用域（按归属语义判定，刻意排除派生级表）。
#: 合并二者会把「有 project_id」误当成「应建归属外键」，正是 a9b8 拒绝做的事。
PROJECT_SCOPED: tuple[tuple[str, str], ...] = (
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


class Step(NamedTuple):
    """一步一条语句（口径同 a9b8：单语句才能精确定位失败点）。"""

    code: str
    title: str
    sql: str
    note: str = ""


def _policy(table: str) -> str:
    return f"quarantine_restrictive_{table}"


def upgrade_steps() -> list[Step]:
    steps: list[Step] = []

    # ── Q0 前置断言 ────────────────────────────────────────────────────
    steps.append(
        Step(
            "Q0",
            "前置断言：a9b8/b0c9 已生效、projects.entity_id 容得下隔离键",
            f"""
            DO $do$
            DECLARE v_len integer; missing text;
            BEGIN
                IF to_regclass('{ORGS}') IS NULL THEN
                    RAISE EXCEPTION 'PREFLIGHT: {ORGS} 不存在——须先执行 a9b8c7d6e5f4';
                END IF;
                IF to_regclass('{PLANNING}.ownership_repair_map') IS NULL THEN
                    RAISE EXCEPTION 'PREFLIGHT: ownership_repair_map 不存在——须先执行 b0c9d8e7f6a5';
                END IF;
                SELECT character_maximum_length INTO v_len
                  FROM information_schema.columns
                 WHERE table_schema = 'project_planning'
                   AND table_name = 'projects'
                   AND column_name = 'entity_id';
                IF v_len IS NULL OR v_len < 46 THEN
                    RAISE EXCEPTION
                        'PREFLIGHT: projects.entity_id 长度 % 不足以容纳 quarantine-<uuid>(46)',
                        v_len;
                END IF;
                SELECT string_agg(c, ', ') INTO missing
                  FROM unnest(ARRAY['status', 'project_tier', 'project_name', 'organization_id', 'tenant_id']) c
                 WHERE NOT EXISTS (
                     SELECT 1 FROM information_schema.columns
                      WHERE table_schema = 'project_planning'
                        AND table_name = 'projects'
                        AND column_name = c);
                IF missing IS NOT NULL THEN
                    RAISE EXCEPTION 'PREFLIGHT: projects 缺少列: %', missing;
                END IF;
            END $do$;
            """,
            "fail-closed：前置不成立就中止，不靠 IF EXISTS 静默跳过",
        )
    )

    # ── Q1 判定函数（单一事实源）────────────────────────────────────────
    steps.append(
        Step(
            "Q1",
            f"判定函数 {PLANNING}.is_quarantine_project(text)",
            f"""
            CREATE OR REPLACE FUNCTION {PLANNING}.is_quarantine_project(p_entity_id text)
            RETURNS boolean
            LANGUAGE sql
            IMMUTABLE
            STRICT
            PARALLEL SAFE
            AS $fn$
                SELECT left(p_entity_id, {len(QUARANTINE_PREFIX)}) = '{QUARANTINE_PREFIX}'
            $fn$;
            """,
            "用 left() 而非 LIKE：LIKE 里 % 与 _ 是通配符，前缀判定不该引入歧义；"
            "IMMUTABLE + STRICT 使 RLS 策略可稳定内联",
        )
    )

    # ── Q2 终态约束（要求 2：禁止改回 ACTIVE）──────────────────────────
    steps.append(
        Step(
            "Q2",
            "CHECK：隔离项目只能是 ARCHIVED（禁止改回 ACTIVE 的数据库层保证）",
            f"""
            DO $do$ BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                     WHERE conname = 'ck_projects_quarantine_terminal'
                       AND conrelid = '{PROJECTS}'::regclass
                ) THEN
                    ALTER TABLE {PROJECTS}
                        ADD CONSTRAINT ck_projects_quarantine_terminal
                        CHECK (
                            NOT {PLANNING}.is_quarantine_project(entity_id)
                            OR status = 'ARCHIVED'
                        );
                END IF;
            END $do$;
            """,
            "要求 2「禁止被状态机推进回 ACTIVE」：状态机是第一道，CHECK 是最后一道，"
            "后者不依赖任何应用代码路径",
        )
    )

    # ── Q3 审计角色 ────────────────────────────────────────────────────
    steps.append(
        Step(
            "Q3",
            f"审计主体角色 {AUDIT_ROLE}（NOLOGIN，不参与业务连接）",
            f"""
            DO $do$ BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{AUDIT_ROLE}') THEN
                    CREATE ROLE {AUDIT_ROLE} NOLOGIN;
                END IF;
                EXECUTE 'GRANT USAGE ON SCHEMA project_planning TO {AUDIT_ROLE}';
                EXECUTE 'GRANT SELECT ON {PROJECTS} TO {AUDIT_ROLE}';
            END $do$;
            """,
            "D-10.3 要求 6 需要「审计主体可见/可写」这一侧的对照主体；"
            "此前只有 svc_*（业务）与 afs_data_repair（修复），缺少审计身份，"
            "导致该验收项无法构造。**降级不撤该角色**：角色可能已被其他迁移授予权限，"
            "撤角色属独立授权面，不在本迁移的降级范围内",
        )
    )

    # ── Q4 每组织一个隔离项目（要求 1）──────────────────────────────────
    steps.append(
        Step(
            "Q4",
            "为每个已有 organization 物化一个隔离项目（幂等）",
            f"""
            INSERT INTO {PROJECTS} (
                entity_id, entity_type, schema_version, entity_version,
                status, owner_module, created_at, created_by, updated_at,
                audit_ref, project_name, project_tier, tenant_id, organization_id
            )
            SELECT '{QUARANTINE_PREFIX}' || o.organization_id::text,
                   'Project',
                   1,
                   1,
                   'ARCHIVED',
                   'project_planning',
                   now(),
                   '(system) ownership-migration',
                   now(),
                   NULL,
                   '数据隔离项目（非业务）',
                   'B',
                   o.tenant_id,
                   o.organization_id
              FROM {ORGS} o
            ON CONFLICT (entity_id) DO NOTHING
            """,
            "要求 1：真实 projects 行，entity_id = 'quarantine-' || organization_id；"
            "要求 2：status=ARCHIVED、created_by 固定串、project_name 标注；"
            "空库时插入 0 行——这是正确结果，不是失败",
        )
    )

    # ── Q5 projects 表自身的 RESTRICTIVE 策略（要求 3/4）────────────────
    # DROP 与 CREATE 必须分成两个 Step：asyncpg 走 prepared statement，
    # 一次只能提交**一条**命令，多命令会直接 PostgresSyntaxError。
    # 这不是风格问题，是本项目踩过的坑（见 a9b8 的「单语句」注释）。
    steps.append(
        Step(
            "Q5a",
            f"移除 {PROJECTS} 的旧隔离策略（幂等前置）",
            f"DROP POLICY IF EXISTS quarantine_restrictive_projects ON {PROJECTS}",
            "CREATE POLICY 没有 IF NOT EXISTS，故先 DROP 才能重复执行",
        )
    )
    steps.append(
        Step(
            "Q5b",
            f"{PROJECTS} 的 RESTRICTIVE 策略（业务侧不可见、不可写）",
            f"""
            CREATE POLICY quarantine_restrictive_projects ON {PROJECTS}
                AS RESTRICTIVE
                FOR ALL
                TO PUBLIC
                USING (
                    NOT {PLANNING}.is_quarantine_project(entity_id)
                    OR current_setting('{INCLUDE_GUC}', true) = 'on'
                )
                WITH CHECK (
                    NOT {PLANNING}.is_quarantine_project(entity_id)
                    OR pg_has_role(current_user, '{REPAIR_ROLE}', 'member')
                    OR (
                        current_setting('{INCLUDE_GUC}', true) = 'on'
                        AND pg_has_role(current_user, '{AUDIT_ROLE}', 'member')
                    )
                )
            """,
            "RESTRICTIVE 与既有 PERMISSIVE 是 AND 关系，故即使业务把 "
            "app.current_projects 设为 '*'，隔离项目依然不可见——这是结构性保证，"
            "不依赖白名单恰好漏掉它。"
            "写侧刻意不含 GUC 单独的路径：只见 GUC、不属审计主体，仍不可写",
        )
    )

    # ── Q6 业务表的 RESTRICTIVE 策略（要求 3/4 的作用域）────────────────
    for schema, table in PROJECT_SCOPED:
        qualified = f"{schema}.{table}"
        steps.append(
            Step(
                "Q6a",
                f"{qualified}：移除旧隔离策略（幂等前置）",
                f"DROP POLICY IF EXISTS {_policy(table)} ON {qualified}",
            )
        )
        steps.append(
            Step(
                "Q6b",
                f"{qualified}：指向隔离项目的行对业务不可见不可写",
                f"""
                CREATE POLICY {_policy(table)} ON {qualified}
                    AS RESTRICTIVE
                    FOR ALL
                    TO PUBLIC
                    USING (
                        project_id IS NULL
                        OR NOT {PLANNING}.is_quarantine_project(project_id)
                        OR pg_has_role(current_user, '{REPAIR_ROLE}', 'member')
                        OR current_setting('{INCLUDE_GUC}', true) = 'on'
                    )
                    WITH CHECK (
                        project_id IS NULL
                        OR NOT {PLANNING}.is_quarantine_project(project_id)
                        OR pg_has_role(current_user, '{REPAIR_ROLE}', 'member')
                        OR (
                            current_setting('{INCLUDE_GUC}', true) = 'on'
                            AND pg_has_role(current_user, '{AUDIT_ROLE}', 'member')
                        )
                    )
                """,
                "读侧允许 repair 看见（否则订正无从定位，且 RLS 会把它静默过滤成 "
                "UPDATE 0，见 ADR D-10.2.3 陷阱 b）；"
                "写侧允 repair 写入隔离区（订正的本质正是「把行移入隔离区」），"
                "但要求 4 的「业务主体不可写」仍然成立——repair 不是业务主体",
            )
        )

    # ── Q7 注释（可发现性）──────────────────────────────────────────────
    steps.append(
        Step(
            "Q7",
            "为隔离相关对象加注释，避免后来者当成普通项目",
            f"""
            DO $do$ BEGIN
                EXECUTE $c$COMMENT ON FUNCTION {PLANNING}.is_quarantine_project(text)
                    IS '判定 entity_id 是否为隔离项目键（D-10.3）。隔离语义的唯一判定点，禁止在别处重复实现前缀比较。'$c$;
                EXECUTE $c$COMMENT ON CONSTRAINT ck_projects_quarantine_terminal ON {PROJECTS}
                    IS 'D-10.3 要求 2：隔离项目只能是 ARCHIVED 终态，禁止改回 ACTIVE。'$c$;
            END $do$;
            """,
            "注释是给「半年后想把它改成 ACTIVE 的人」看的，属于机制的一部分",
        )
    )

    return steps


def downgrade_steps() -> list[Step]:
    steps: list[Step] = []
    for schema, table in PROJECT_SCOPED:
        steps.append(
            Step(
                "D",
                f"移除 {schema}.{table} 的隔离 RESTRICTIVE 策略",
                f"DROP POLICY IF EXISTS {_policy(table)} ON {schema}.{table}",
            )
        )
    steps.append(
        Step(
            "D",
            f"移除 {PROJECTS} 的隔离 RESTRICTIVE 策略",
            f"DROP POLICY IF EXISTS quarantine_restrictive_projects ON {PROJECTS}",
        )
    )
    steps.append(
        Step(
            "D",
            "删除 CHECK 约束",
            f"ALTER TABLE {PROJECTS} DROP CONSTRAINT IF EXISTS ck_projects_quarantine_terminal",
        )
    )
    # 顺序不能颠倒：DELETE 用 is_quarantine_project 判定，函数必须先活着。
    # 首次编写时把 DELETE 放在 DROP FUNCTION 之后，降级直接
    # 「function does not exist」——降级路径没有被端到端跑过就会这样。
    steps.append(
        Step(
            "D",
            "删除隔离项目行（仅在无引用时；被哨兵行引用则 23503，属正确拒绝）",
            f"""
            DELETE FROM {PROJECTS}
             WHERE {PLANNING}.is_quarantine_project(entity_id);
            """,
            "必须早于 DROP FUNCTION；被业务行引用时依赖外键 NO ACTION 天然拒绝",
        )
    )
    steps.append(
        Step(
            "D",
            "删除判定函数（先确认已无策略引用）",
            f"""
            DO $do$ DECLARE n integer; BEGIN
                SELECT count(*) INTO n FROM pg_policies
                 WHERE schemaname || '.' || tablename = '{PROJECTS}'
                   AND policyname = 'quarantine_restrictive_projects';
                IF n > 0 THEN
                    RAISE EXCEPTION '仍有策略引用判定函数，拒绝 DROP FUNCTION';
                END IF;
                DROP FUNCTION IF EXISTS {PLANNING}.is_quarantine_project(text);
            END $do$;
            """,
        )
    )
    return steps


def upgrade() -> None:
    for step in upgrade_steps():
        op.execute(step.sql)


def downgrade() -> None:
    for step in downgrade_steps():
        op.execute(step.sql)
