"""
补齐迁移 ②：不可变守卫的「受控例外」（D-10.2 实现层）。

本文件由 docs/audits/mig-recon/gen_guard_exception_migration.py 生成。
**请勿手改**。要改判定逻辑 → 改共享函数 _ALLOWS_FN；要改守卫原文 → 改来源库后重新生成。
迁移内**不含任何运行期字符串改写**：GUARD_PATCHED 就是生成时验证过的完整语句。

为什么必须有这一层
------------------
D-10.1 要求 14 表归属闭环（VALIDATE 必须成功），而订正归属列会撞上各表的
不可变守卫（ADR-R11/R27）→ P0001。若不提供例外，订正只能前向新建版本，
旧行仍在且仍违规 → VALIDATE 必失败（T3 D3e）→ 闭环不可达。

为什么不是 DISABLE TRIGGER
--------------------------
禁守卫使豁免面变成「整表 × 所有列 × 所有会话」，且需 ACCESS EXCLUSIVE 锁。
受控例外把豁免面压到「单行 × 归属三列 × 单工单 × 单角色」，且可逐行审计。

四条件（缺一即不放行，判定唯一实现在 ownership_repair_allows）
------------------------------------------------------------
1. 会话角色是 afs_data_repair 成员（pg_has_role 读真实身份，GUC 不可伪造）
2. 会话设置了 app.ownership_repair_ticket
3. 变更列 ⊆ {project_id, organization_id, tenant_id}（函数自算，不接受调用方传入）
4. ownership_repair_map 存在 (表, 行标识, 工单) 匹配且 applied_at IS NULL

范围说明
--------
13 张表 = 归属矩阵 13 张域表中**有不可变守卫**的那些（projects 无守卫，不需接入）。
另含 asset_versions（归属矩阵第 15 张，T3 代表表）与 qc_reports（有归属列但无 entity_id）。
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "b0c9d8e7f6a5"
down_revision: str | Sequence[str] | None = "a9b8c7d6e5f4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

REPAIR_ROLE = "afs_data_repair"
MAP_TABLE = "project_planning.ownership_repair_map"


# 生成顺序即执行顺序（与校验脚本的期望顺序一致）
GUARD_ORDER: tuple[str, ...] = (
    'asset_service.asset_versions',
    'asset_service.delivery_manifests',
    'asset_service.qc_reports',
    'generation_execution.generation_tasks',
    'generation_execution.production_routes',
    'generation_execution.shot_route_selections',
    'qc_governance.gate_decisions',
    'qc_governance.qc_review_sessions',
    'shot_planning.constraint_packs',
    'shot_planning.shot_specs',
    'shot_planning.shot_visual_plans',
    'workflow.approval_flows',
    'workflow.approval_steps',
)

# 原文（导出时刻的真实定义，逐字保留）：downgrade 用这一份
GUARD_ORIGINAL: dict[str, str] = {
    # asset_service.asset_versions  行标识=entity_id
    'asset_service.asset_versions': r'''CREATE OR REPLACE FUNCTION asset_service.asset_versions_guard()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
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
$function$''',
    # asset_service.delivery_manifests  行标识=entity_id
    'asset_service.delivery_manifests': r'''CREATE OR REPLACE FUNCTION asset_service.delivery_manifests_guard()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
DECLARE
    col text;
    old_status text;
    new_status text;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION
            'delivery_manifests 是 append-only 交付清单，禁止删除（每个 Manifest 版本不可变，'
            '修正须新建 manifest_id）';
    END IF;

    old_status := to_jsonb(OLD) ->> 'status';
    new_status := to_jsonb(NEW) ->> 'status';
    IF old_status IS DISTINCT FROM new_status THEN
        IF NOT EXISTS (
            SELECT 1 FROM asset_service.delivery_manifest_status_transitions t
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
        WHERE attrelid = 'asset_service.delivery_manifests'::regclass
          AND attnum > 0
          AND NOT attisdropped
    LOOP
        IF col NOT IN ('status', 'entity_version', 'updated_at', 'audit_ref') THEN
            IF (to_jsonb(OLD) ->> col) IS DISTINCT FROM (to_jsonb(NEW) ->> col) THEN
                RAISE EXCEPTION
                    'delivery_manifests.% 为不可变内容列，禁止改写（每个 Manifest 版本不可变）',
                    col;
            END IF;
        END IF;
    END LOOP;

    RETURN NEW;
END;
$function$''',
    # asset_service.qc_reports  行标识=qc_report_id
    'asset_service.qc_reports': r'''CREATE OR REPLACE FUNCTION asset_service.qc_reports_guard()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
BEGIN
    RAISE EXCEPTION
        'qc_reports 是追加型检测证据，禁止 %（ADR-R11）', TG_OP;
END;
$function$''',
    # generation_execution.generation_tasks  行标识=entity_id
    'generation_execution.generation_tasks': r'''CREATE OR REPLACE FUNCTION generation_execution.generation_tasks_guard()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
        DECLARE
            col text;
            old_status text;
            new_status text;
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION
                    'generation_execution.generation_tasks 业务对象不可物理删除';
            END IF;

            old_status := to_jsonb(OLD) ->> 'status';
            new_status := to_jsonb(NEW) ->> 'status';
            IF old_status IS DISTINCT FROM new_status THEN
                IF NOT EXISTS (
                    SELECT 1 FROM generation_execution.generation_task_status_transitions t
                    WHERE t.from_status = old_status AND t.to_status = new_status
                ) THEN
                    RAISE EXCEPTION
                        '非法状态转移 % -> %（未登记，ADR-R20）',
                        old_status, new_status;
                END IF;
            END IF;

            FOR col IN
                SELECT attname
                FROM pg_attribute
                WHERE attrelid = 'generation_execution.generation_tasks'::regclass
                  AND attnum > 0
                  AND NOT attisdropped
            LOOP
                IF col IN ('status', 'entity_version', 'updated_at', 'audit_ref') THEN
                    CONTINUE;
                END IF;
                IF col IN ('result_ref', 'started_at', 'completed_at', 'lease_owner', 'lease_expires_at') THEN
                    CONTINUE;
                END IF;
                IF (to_jsonb(OLD) ->> col) IS DISTINCT FROM (to_jsonb(NEW) ->> col) THEN
                    RAISE EXCEPTION
                        'generation_execution.generation_tasks.% 为不可变内容列，禁止改写', col;
                END IF;
            END LOOP;

            RETURN NEW;
        END;
        $function$''',
    # generation_execution.production_routes  行标识=entity_id
    'generation_execution.production_routes': r'''CREATE OR REPLACE FUNCTION generation_execution.production_routes_guard()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
        DECLARE
            col text;
            old_status text;
            new_status text;
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION
                    'generation_execution.production_routes 业务对象不可物理删除';
            END IF;

            old_status := to_jsonb(OLD) ->> 'status';
            new_status := to_jsonb(NEW) ->> 'status';
            IF old_status IS DISTINCT FROM new_status THEN
                IF NOT EXISTS (
                    SELECT 1 FROM generation_execution.production_route_status_transitions t
                    WHERE t.from_status = old_status AND t.to_status = new_status
                ) THEN
                    RAISE EXCEPTION
                        '非法状态转移 % -> %（未登记，ADR-R20）',
                        old_status, new_status;
                END IF;
            END IF;

            FOR col IN
                SELECT attname
                FROM pg_attribute
                WHERE attrelid = 'generation_execution.production_routes'::regclass
                  AND attnum > 0
                  AND NOT attisdropped
            LOOP
                IF col IN ('status', 'entity_version', 'updated_at', 'audit_ref') THEN
                    CONTINUE;
                END IF;
                IF col IN ('verified_by', 'verified_at') THEN
                    CONTINUE;
                END IF;
                IF (to_jsonb(OLD) ->> col) IS DISTINCT FROM (to_jsonb(NEW) ->> col) THEN
                    RAISE EXCEPTION
                        'generation_execution.production_routes.% 为不可变内容列，禁止改写', col;
                END IF;
            END LOOP;

            RETURN NEW;
        END;
        $function$''',
    # generation_execution.shot_route_selections  行标识=entity_id
    'generation_execution.shot_route_selections': r'''CREATE OR REPLACE FUNCTION generation_execution.shot_route_selections_guard()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION
                    'generation_execution.shot_route_selections 业务对象不可物理删除';
            END IF;
            RAISE EXCEPTION
                'generation_execution.shot_route_selections 为受理事实（生效路线），禁止改写'
                '（单一路线生成，V205-R14-A9）';
        END;
        $function$''',
    # qc_governance.gate_decisions  行标识=entity_id
    'qc_governance.gate_decisions': r'''CREATE OR REPLACE FUNCTION qc_governance.gate_decisions_guard()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
BEGIN
    RAISE EXCEPTION
        'gate_decisions 是 append-only Gate 裁决事实，禁止 % （裁决不可改写，'
        '修正须新建一条裁决并附新证据）', TG_OP;
END;
$function$''',
    # qc_governance.qc_review_sessions  行标识=entity_id
    'qc_governance.qc_review_sessions': r'''CREATE OR REPLACE FUNCTION qc_governance.qc_review_sessions_guard()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
        DECLARE
            col text;
            old_status text;
            new_status text;
            idx integer;
            old_len integer;
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION
                    'qc_governance.qc_review_sessions 是 QC 复查证据，禁止物理删除'
                    '（结论与缺陷是 Gate 追溯的依据）';
            END IF;

            old_status := to_jsonb(OLD) ->> 'status';
            new_status := to_jsonb(NEW) ->> 'status';
            IF old_status IS DISTINCT FROM new_status THEN
                IF NOT EXISTS (
                    SELECT 1 FROM qc_governance.qc_review_session_status_transitions t
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
                WHERE attrelid = 'qc_governance.qc_review_sessions'::regclass
                  AND attnum > 0
                  AND NOT attisdropped
            LOOP
                IF col IN ('status', 'entity_version', 'updated_at', 'audit_ref') THEN
                    CONTINUE;
                END IF;
                IF col IN ('decisions', 'reviewers') THEN
                    CONTINUE;
                END IF;
                IF (to_jsonb(OLD) ->> col) IS DISTINCT FROM (to_jsonb(NEW) ->> col) THEN
                    RAISE EXCEPTION
                        'qc_governance.qc_review_sessions.% 为不可变内容列，禁止改写', col;
                END IF;
            END LOOP;

            -- decisions 只允许追加（21.1）：新数组是旧数组的前缀超集。
            -- 逐项比较而不是只比长度：整体改写会保持长度不变，只比长度等于
            -- 没检查。改判走"再追加一条"，历史因此永远可追溯。
            old_len := jsonb_array_length(OLD.decisions);
            IF jsonb_array_length(NEW.decisions) < old_len THEN
                RAISE EXCEPTION
                    'qc_governance.qc_review_sessions.decisions 只允许追加，不允许删除'
                    '（共 % 条结论被缩短）', old_len;
            END IF;
            FOR idx IN 0 .. old_len - 1 LOOP
                IF (NEW.decisions -> idx) IS DISTINCT FROM (OLD.decisions -> idx) THEN
                    RAISE EXCEPTION
                        'qc_governance.qc_review_sessions.decisions[%] 不允许改写'
                        '（结论是 append-only 事实，改判须追加新结论）', idx;
                END IF;
            END LOOP;

            -- reviewers 只增不减：复核参与人是事实，不能被后来的写入抹掉。
            IF NOT (NEW.reviewers @> OLD.reviewers) THEN
                RAISE EXCEPTION
                    'qc_governance.qc_review_sessions.reviewers 只允许追加参与人，不允许移除';
            END IF;

            RETURN NEW;
        END;
        $function$''',
    # shot_planning.constraint_packs  行标识=entity_id
    'shot_planning.constraint_packs': r'''CREATE OR REPLACE FUNCTION shot_planning.constraint_packs_guard()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
        DECLARE
            col text;
            old_status text;
            new_status text;
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION
                    'shot_planning.constraint_packs 业务对象不可物理删除（ADR-R27）';
            END IF;

            old_status := to_jsonb(OLD) ->> 'status';
            new_status := to_jsonb(NEW) ->> 'status';

    IF old_status IS DISTINCT FROM new_status THEN
        IF NOT EXISTS (
            SELECT 1 FROM shot_planning.constraint_pack_status_transitions t
            WHERE t.from_status = old_status AND t.to_status = new_status
        ) THEN
            RAISE EXCEPTION
                '非法状态转移 % -> %（未在 constraint_pack_status_transitions 登记，ADR-R20）',
                old_status, new_status;
        END IF;
    END IF;

            FOR col IN
                SELECT attname
                FROM pg_attribute
                WHERE attrelid = 'shot_planning.constraint_packs'::regclass
                  AND attnum > 0
                  AND NOT attisdropped
            LOOP
                IF col IN ('status', 'entity_version', 'updated_at', 'audit_ref') THEN
                    CONTINUE;
                END IF;
                IF col IN ('approved_by', 'approved_at') THEN
                    CONTINUE;
                END IF;
                IF (to_jsonb(OLD) ->> col) IS DISTINCT FROM (to_jsonb(NEW) ->> col) THEN
                    RAISE EXCEPTION
                        'shot_planning.constraint_packs.% 为不可变内容列，禁止改写（ADR-R27/C2）', col;
                END IF;
            END LOOP;

            RETURN NEW;
        END;
        $function$''',
    # shot_planning.shot_specs  行标识=entity_id
    'shot_planning.shot_specs': r'''CREATE OR REPLACE FUNCTION shot_planning.shot_specs_guard()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
        DECLARE
            col text;
            old_status text;
            new_status text;
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION
                    'shot_planning.shot_specs 业务对象不可物理删除（ADR-R27）';
            END IF;

            old_status := to_jsonb(OLD) ->> 'status';
            new_status := to_jsonb(NEW) ->> 'status';

            FOR col IN
                SELECT attname
                FROM pg_attribute
                WHERE attrelid = 'shot_planning.shot_specs'::regclass
                  AND attnum > 0
                  AND NOT attisdropped
            LOOP
                IF col IN ('status', 'entity_version', 'updated_at', 'audit_ref') THEN
                    CONTINUE;
                END IF;
                IF col IN ('locked_dimensions', 'locked_by', 'locked_at', 'lock_rationale') THEN
                    CONTINUE;
                END IF;
                IF (to_jsonb(OLD) ->> col) IS DISTINCT FROM (to_jsonb(NEW) ->> col) THEN
                    RAISE EXCEPTION
                        'shot_planning.shot_specs.% 为不可变内容列，禁止改写（ADR-R27/C2）', col;
                END IF;
            END LOOP;

            RETURN NEW;
        END;
        $function$''',
    # shot_planning.shot_visual_plans  行标识=entity_id
    'shot_planning.shot_visual_plans': r'''CREATE OR REPLACE FUNCTION shot_planning.shot_visual_plans_guard()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
        DECLARE
            col text;
            old_status text;
            new_status text;
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION
                    'shot_planning.shot_visual_plans 业务对象不可物理删除（ADR-R27）';
            END IF;

            old_status := to_jsonb(OLD) ->> 'status';
            new_status := to_jsonb(NEW) ->> 'status';

    IF old_status IS DISTINCT FROM new_status THEN
        IF NOT EXISTS (
            SELECT 1 FROM shot_planning.shot_visual_plan_status_transitions t
            WHERE t.from_status = old_status AND t.to_status = new_status
        ) THEN
            RAISE EXCEPTION
                '非法状态转移 % -> %（未在 shot_visual_plan_status_transitions 登记，ADR-R20）',
                old_status, new_status;
        END IF;
    END IF;

            FOR col IN
                SELECT attname
                FROM pg_attribute
                WHERE attrelid = 'shot_planning.shot_visual_plans'::regclass
                  AND attnum > 0
                  AND NOT attisdropped
            LOOP
                IF col IN ('status', 'entity_version', 'updated_at', 'audit_ref') THEN
                    CONTINUE;
                END IF;
                IF col IN ('locked_fields', 'open_fields') THEN
                    CONTINUE;
                END IF;
                IF (to_jsonb(OLD) ->> col) IS DISTINCT FROM (to_jsonb(NEW) ->> col) THEN
                    RAISE EXCEPTION
                        'shot_planning.shot_visual_plans.% 为不可变内容列，禁止改写（ADR-R27/C2）', col;
                END IF;
            END LOOP;

            RETURN NEW;
        END;
        $function$''',
    # workflow.approval_flows  行标识=entity_id
    'workflow.approval_flows': r'''CREATE OR REPLACE FUNCTION workflow.approval_flows_guard()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
    BEGIN
        RAISE EXCEPTION
            'approval_flows 是 append-only 审批事实，禁止 %（修正须追加新事实）', TG_OP;
    END;
    $function$''',
    # workflow.approval_steps  行标识=entity_id
    'workflow.approval_steps': r'''CREATE OR REPLACE FUNCTION workflow.approval_steps_guard()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
    BEGIN
        RAISE EXCEPTION
            'approval_steps 是 append-only 审批事实，禁止 %（修正须追加新事实）', TG_OP;
    END;
    $function$''',
}

# 注入接入点后的完整语句（upgrade 用这一份，生成时已逐字校验）
GUARD_PATCHED: dict[str, str] = {
    # asset_service.asset_versions  行标识=entity_id
    'asset_service.asset_versions': r'''CREATE OR REPLACE FUNCTION asset_service.asset_versions_guard()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
DECLARE
    col text;
    old_status text;
    new_status text;
BEGIN
    -- 受控例外（ADR-R40 §13 D-10.2，v1.4.5 读法裁定后）。
    -- 四条件同时满足才放行；判定逻辑的唯一实现在
    -- project_planning.ownership_repair_allows()，此处只接入、不复制条件。
    IF TG_OP = 'UPDATE' THEN
        IF project_planning.ownership_repair_allows(
            TG_RELID, to_jsonb(OLD), to_jsonb(NEW),
            to_jsonb(OLD) ->> 'entity_id'
        ) THEN
            RETURN NEW;
        END IF;
    END IF;

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
$function$''',
    # asset_service.delivery_manifests  行标识=entity_id
    'asset_service.delivery_manifests': r'''CREATE OR REPLACE FUNCTION asset_service.delivery_manifests_guard()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
DECLARE
    col text;
    old_status text;
    new_status text;
BEGIN
    -- 受控例外（ADR-R40 §13 D-10.2，v1.4.5 读法裁定后）。
    -- 四条件同时满足才放行；判定逻辑的唯一实现在
    -- project_planning.ownership_repair_allows()，此处只接入、不复制条件。
    IF TG_OP = 'UPDATE' THEN
        IF project_planning.ownership_repair_allows(
            TG_RELID, to_jsonb(OLD), to_jsonb(NEW),
            to_jsonb(OLD) ->> 'entity_id'
        ) THEN
            RETURN NEW;
        END IF;
    END IF;

    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION
            'delivery_manifests 是 append-only 交付清单，禁止删除（每个 Manifest 版本不可变，'
            '修正须新建 manifest_id）';
    END IF;

    old_status := to_jsonb(OLD) ->> 'status';
    new_status := to_jsonb(NEW) ->> 'status';
    IF old_status IS DISTINCT FROM new_status THEN
        IF NOT EXISTS (
            SELECT 1 FROM asset_service.delivery_manifest_status_transitions t
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
        WHERE attrelid = 'asset_service.delivery_manifests'::regclass
          AND attnum > 0
          AND NOT attisdropped
    LOOP
        IF col NOT IN ('status', 'entity_version', 'updated_at', 'audit_ref') THEN
            IF (to_jsonb(OLD) ->> col) IS DISTINCT FROM (to_jsonb(NEW) ->> col) THEN
                RAISE EXCEPTION
                    'delivery_manifests.% 为不可变内容列，禁止改写（每个 Manifest 版本不可变）',
                    col;
            END IF;
        END IF;
    END LOOP;

    RETURN NEW;
END;
$function$''',
    # asset_service.qc_reports  行标识=qc_report_id
    'asset_service.qc_reports': r'''CREATE OR REPLACE FUNCTION asset_service.qc_reports_guard()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
BEGIN
    -- 受控例外（ADR-R40 §13 D-10.2，v1.4.5 读法裁定后）。
    -- 四条件同时满足才放行；判定逻辑的唯一实现在
    -- project_planning.ownership_repair_allows()，此处只接入、不复制条件。
    IF TG_OP = 'UPDATE' THEN
        IF project_planning.ownership_repair_allows(
            TG_RELID, to_jsonb(OLD), to_jsonb(NEW),
            to_jsonb(OLD) ->> 'qc_report_id'
        ) THEN
            RETURN NEW;
        END IF;
    END IF;

    RAISE EXCEPTION
        'qc_reports 是追加型检测证据，禁止 %（ADR-R11）', TG_OP;
END;
$function$''',
    # generation_execution.generation_tasks  行标识=entity_id
    'generation_execution.generation_tasks': r'''CREATE OR REPLACE FUNCTION generation_execution.generation_tasks_guard()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
        DECLARE
            col text;
            old_status text;
            new_status text;
        BEGIN
            -- 受控例外（ADR-R40 §13 D-10.2，v1.4.5 读法裁定后）。
            -- 四条件同时满足才放行；判定逻辑的唯一实现在
            -- project_planning.ownership_repair_allows()，此处只接入、不复制条件。
            IF TG_OP = 'UPDATE' THEN
                IF project_planning.ownership_repair_allows(
                    TG_RELID, to_jsonb(OLD), to_jsonb(NEW),
                    to_jsonb(OLD) ->> 'entity_id'
                ) THEN
                    RETURN NEW;
                END IF;
            END IF;

            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION
                    'generation_execution.generation_tasks 业务对象不可物理删除';
            END IF;

            old_status := to_jsonb(OLD) ->> 'status';
            new_status := to_jsonb(NEW) ->> 'status';
            IF old_status IS DISTINCT FROM new_status THEN
                IF NOT EXISTS (
                    SELECT 1 FROM generation_execution.generation_task_status_transitions t
                    WHERE t.from_status = old_status AND t.to_status = new_status
                ) THEN
                    RAISE EXCEPTION
                        '非法状态转移 % -> %（未登记，ADR-R20）',
                        old_status, new_status;
                END IF;
            END IF;

            FOR col IN
                SELECT attname
                FROM pg_attribute
                WHERE attrelid = 'generation_execution.generation_tasks'::regclass
                  AND attnum > 0
                  AND NOT attisdropped
            LOOP
                IF col IN ('status', 'entity_version', 'updated_at', 'audit_ref') THEN
                    CONTINUE;
                END IF;
                IF col IN ('result_ref', 'started_at', 'completed_at', 'lease_owner', 'lease_expires_at') THEN
                    CONTINUE;
                END IF;
                IF (to_jsonb(OLD) ->> col) IS DISTINCT FROM (to_jsonb(NEW) ->> col) THEN
                    RAISE EXCEPTION
                        'generation_execution.generation_tasks.% 为不可变内容列，禁止改写', col;
                END IF;
            END LOOP;

            RETURN NEW;
        END;
        $function$''',
    # generation_execution.production_routes  行标识=entity_id
    'generation_execution.production_routes': r'''CREATE OR REPLACE FUNCTION generation_execution.production_routes_guard()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
        DECLARE
            col text;
            old_status text;
            new_status text;
        BEGIN
            -- 受控例外（ADR-R40 §13 D-10.2，v1.4.5 读法裁定后）。
            -- 四条件同时满足才放行；判定逻辑的唯一实现在
            -- project_planning.ownership_repair_allows()，此处只接入、不复制条件。
            IF TG_OP = 'UPDATE' THEN
                IF project_planning.ownership_repair_allows(
                    TG_RELID, to_jsonb(OLD), to_jsonb(NEW),
                    to_jsonb(OLD) ->> 'entity_id'
                ) THEN
                    RETURN NEW;
                END IF;
            END IF;

            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION
                    'generation_execution.production_routes 业务对象不可物理删除';
            END IF;

            old_status := to_jsonb(OLD) ->> 'status';
            new_status := to_jsonb(NEW) ->> 'status';
            IF old_status IS DISTINCT FROM new_status THEN
                IF NOT EXISTS (
                    SELECT 1 FROM generation_execution.production_route_status_transitions t
                    WHERE t.from_status = old_status AND t.to_status = new_status
                ) THEN
                    RAISE EXCEPTION
                        '非法状态转移 % -> %（未登记，ADR-R20）',
                        old_status, new_status;
                END IF;
            END IF;

            FOR col IN
                SELECT attname
                FROM pg_attribute
                WHERE attrelid = 'generation_execution.production_routes'::regclass
                  AND attnum > 0
                  AND NOT attisdropped
            LOOP
                IF col IN ('status', 'entity_version', 'updated_at', 'audit_ref') THEN
                    CONTINUE;
                END IF;
                IF col IN ('verified_by', 'verified_at') THEN
                    CONTINUE;
                END IF;
                IF (to_jsonb(OLD) ->> col) IS DISTINCT FROM (to_jsonb(NEW) ->> col) THEN
                    RAISE EXCEPTION
                        'generation_execution.production_routes.% 为不可变内容列，禁止改写', col;
                END IF;
            END LOOP;

            RETURN NEW;
        END;
        $function$''',
    # generation_execution.shot_route_selections  行标识=entity_id
    'generation_execution.shot_route_selections': r'''CREATE OR REPLACE FUNCTION generation_execution.shot_route_selections_guard()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
        BEGIN
            -- 受控例外（ADR-R40 §13 D-10.2，v1.4.5 读法裁定后）。
            -- 四条件同时满足才放行；判定逻辑的唯一实现在
            -- project_planning.ownership_repair_allows()，此处只接入、不复制条件。
            IF TG_OP = 'UPDATE' THEN
                IF project_planning.ownership_repair_allows(
                    TG_RELID, to_jsonb(OLD), to_jsonb(NEW),
                    to_jsonb(OLD) ->> 'entity_id'
                ) THEN
                    RETURN NEW;
                END IF;
            END IF;

            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION
                    'generation_execution.shot_route_selections 业务对象不可物理删除';
            END IF;
            RAISE EXCEPTION
                'generation_execution.shot_route_selections 为受理事实（生效路线），禁止改写'
                '（单一路线生成，V205-R14-A9）';
        END;
        $function$''',
    # qc_governance.gate_decisions  行标识=entity_id
    'qc_governance.gate_decisions': r'''CREATE OR REPLACE FUNCTION qc_governance.gate_decisions_guard()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
BEGIN
    -- 受控例外（ADR-R40 §13 D-10.2，v1.4.5 读法裁定后）。
    -- 四条件同时满足才放行；判定逻辑的唯一实现在
    -- project_planning.ownership_repair_allows()，此处只接入、不复制条件。
    IF TG_OP = 'UPDATE' THEN
        IF project_planning.ownership_repair_allows(
            TG_RELID, to_jsonb(OLD), to_jsonb(NEW),
            to_jsonb(OLD) ->> 'entity_id'
        ) THEN
            RETURN NEW;
        END IF;
    END IF;

    RAISE EXCEPTION
        'gate_decisions 是 append-only Gate 裁决事实，禁止 % （裁决不可改写，'
        '修正须新建一条裁决并附新证据）', TG_OP;
END;
$function$''',
    # qc_governance.qc_review_sessions  行标识=entity_id
    'qc_governance.qc_review_sessions': r'''CREATE OR REPLACE FUNCTION qc_governance.qc_review_sessions_guard()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
        DECLARE
            col text;
            old_status text;
            new_status text;
            idx integer;
            old_len integer;
        BEGIN
            -- 受控例外（ADR-R40 §13 D-10.2，v1.4.5 读法裁定后）。
            -- 四条件同时满足才放行；判定逻辑的唯一实现在
            -- project_planning.ownership_repair_allows()，此处只接入、不复制条件。
            IF TG_OP = 'UPDATE' THEN
                IF project_planning.ownership_repair_allows(
                    TG_RELID, to_jsonb(OLD), to_jsonb(NEW),
                    to_jsonb(OLD) ->> 'entity_id'
                ) THEN
                    RETURN NEW;
                END IF;
            END IF;

            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION
                    'qc_governance.qc_review_sessions 是 QC 复查证据，禁止物理删除'
                    '（结论与缺陷是 Gate 追溯的依据）';
            END IF;

            old_status := to_jsonb(OLD) ->> 'status';
            new_status := to_jsonb(NEW) ->> 'status';
            IF old_status IS DISTINCT FROM new_status THEN
                IF NOT EXISTS (
                    SELECT 1 FROM qc_governance.qc_review_session_status_transitions t
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
                WHERE attrelid = 'qc_governance.qc_review_sessions'::regclass
                  AND attnum > 0
                  AND NOT attisdropped
            LOOP
                IF col IN ('status', 'entity_version', 'updated_at', 'audit_ref') THEN
                    CONTINUE;
                END IF;
                IF col IN ('decisions', 'reviewers') THEN
                    CONTINUE;
                END IF;
                IF (to_jsonb(OLD) ->> col) IS DISTINCT FROM (to_jsonb(NEW) ->> col) THEN
                    RAISE EXCEPTION
                        'qc_governance.qc_review_sessions.% 为不可变内容列，禁止改写', col;
                END IF;
            END LOOP;

            -- decisions 只允许追加（21.1）：新数组是旧数组的前缀超集。
            -- 逐项比较而不是只比长度：整体改写会保持长度不变，只比长度等于
            -- 没检查。改判走"再追加一条"，历史因此永远可追溯。
            old_len := jsonb_array_length(OLD.decisions);
            IF jsonb_array_length(NEW.decisions) < old_len THEN
                RAISE EXCEPTION
                    'qc_governance.qc_review_sessions.decisions 只允许追加，不允许删除'
                    '（共 % 条结论被缩短）', old_len;
            END IF;
            FOR idx IN 0 .. old_len - 1 LOOP
                IF (NEW.decisions -> idx) IS DISTINCT FROM (OLD.decisions -> idx) THEN
                    RAISE EXCEPTION
                        'qc_governance.qc_review_sessions.decisions[%] 不允许改写'
                        '（结论是 append-only 事实，改判须追加新结论）', idx;
                END IF;
            END LOOP;

            -- reviewers 只增不减：复核参与人是事实，不能被后来的写入抹掉。
            IF NOT (NEW.reviewers @> OLD.reviewers) THEN
                RAISE EXCEPTION
                    'qc_governance.qc_review_sessions.reviewers 只允许追加参与人，不允许移除';
            END IF;

            RETURN NEW;
        END;
        $function$''',
    # shot_planning.constraint_packs  行标识=entity_id
    'shot_planning.constraint_packs': r'''CREATE OR REPLACE FUNCTION shot_planning.constraint_packs_guard()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
        DECLARE
            col text;
            old_status text;
            new_status text;
        BEGIN
            -- 受控例外（ADR-R40 §13 D-10.2，v1.4.5 读法裁定后）。
            -- 四条件同时满足才放行；判定逻辑的唯一实现在
            -- project_planning.ownership_repair_allows()，此处只接入、不复制条件。
            IF TG_OP = 'UPDATE' THEN
                IF project_planning.ownership_repair_allows(
                    TG_RELID, to_jsonb(OLD), to_jsonb(NEW),
                    to_jsonb(OLD) ->> 'entity_id'
                ) THEN
                    RETURN NEW;
                END IF;
            END IF;

            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION
                    'shot_planning.constraint_packs 业务对象不可物理删除（ADR-R27）';
            END IF;

            old_status := to_jsonb(OLD) ->> 'status';
            new_status := to_jsonb(NEW) ->> 'status';

    IF old_status IS DISTINCT FROM new_status THEN
        IF NOT EXISTS (
            SELECT 1 FROM shot_planning.constraint_pack_status_transitions t
            WHERE t.from_status = old_status AND t.to_status = new_status
        ) THEN
            RAISE EXCEPTION
                '非法状态转移 % -> %（未在 constraint_pack_status_transitions 登记，ADR-R20）',
                old_status, new_status;
        END IF;
    END IF;

            FOR col IN
                SELECT attname
                FROM pg_attribute
                WHERE attrelid = 'shot_planning.constraint_packs'::regclass
                  AND attnum > 0
                  AND NOT attisdropped
            LOOP
                IF col IN ('status', 'entity_version', 'updated_at', 'audit_ref') THEN
                    CONTINUE;
                END IF;
                IF col IN ('approved_by', 'approved_at') THEN
                    CONTINUE;
                END IF;
                IF (to_jsonb(OLD) ->> col) IS DISTINCT FROM (to_jsonb(NEW) ->> col) THEN
                    RAISE EXCEPTION
                        'shot_planning.constraint_packs.% 为不可变内容列，禁止改写（ADR-R27/C2）', col;
                END IF;
            END LOOP;

            RETURN NEW;
        END;
        $function$''',
    # shot_planning.shot_specs  行标识=entity_id
    'shot_planning.shot_specs': r'''CREATE OR REPLACE FUNCTION shot_planning.shot_specs_guard()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
        DECLARE
            col text;
            old_status text;
            new_status text;
        BEGIN
            -- 受控例外（ADR-R40 §13 D-10.2，v1.4.5 读法裁定后）。
            -- 四条件同时满足才放行；判定逻辑的唯一实现在
            -- project_planning.ownership_repair_allows()，此处只接入、不复制条件。
            IF TG_OP = 'UPDATE' THEN
                IF project_planning.ownership_repair_allows(
                    TG_RELID, to_jsonb(OLD), to_jsonb(NEW),
                    to_jsonb(OLD) ->> 'entity_id'
                ) THEN
                    RETURN NEW;
                END IF;
            END IF;

            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION
                    'shot_planning.shot_specs 业务对象不可物理删除（ADR-R27）';
            END IF;

            old_status := to_jsonb(OLD) ->> 'status';
            new_status := to_jsonb(NEW) ->> 'status';

            FOR col IN
                SELECT attname
                FROM pg_attribute
                WHERE attrelid = 'shot_planning.shot_specs'::regclass
                  AND attnum > 0
                  AND NOT attisdropped
            LOOP
                IF col IN ('status', 'entity_version', 'updated_at', 'audit_ref') THEN
                    CONTINUE;
                END IF;
                IF col IN ('locked_dimensions', 'locked_by', 'locked_at', 'lock_rationale') THEN
                    CONTINUE;
                END IF;
                IF (to_jsonb(OLD) ->> col) IS DISTINCT FROM (to_jsonb(NEW) ->> col) THEN
                    RAISE EXCEPTION
                        'shot_planning.shot_specs.% 为不可变内容列，禁止改写（ADR-R27/C2）', col;
                END IF;
            END LOOP;

            RETURN NEW;
        END;
        $function$''',
    # shot_planning.shot_visual_plans  行标识=entity_id
    'shot_planning.shot_visual_plans': r'''CREATE OR REPLACE FUNCTION shot_planning.shot_visual_plans_guard()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
        DECLARE
            col text;
            old_status text;
            new_status text;
        BEGIN
            -- 受控例外（ADR-R40 §13 D-10.2，v1.4.5 读法裁定后）。
            -- 四条件同时满足才放行；判定逻辑的唯一实现在
            -- project_planning.ownership_repair_allows()，此处只接入、不复制条件。
            IF TG_OP = 'UPDATE' THEN
                IF project_planning.ownership_repair_allows(
                    TG_RELID, to_jsonb(OLD), to_jsonb(NEW),
                    to_jsonb(OLD) ->> 'entity_id'
                ) THEN
                    RETURN NEW;
                END IF;
            END IF;

            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION
                    'shot_planning.shot_visual_plans 业务对象不可物理删除（ADR-R27）';
            END IF;

            old_status := to_jsonb(OLD) ->> 'status';
            new_status := to_jsonb(NEW) ->> 'status';

    IF old_status IS DISTINCT FROM new_status THEN
        IF NOT EXISTS (
            SELECT 1 FROM shot_planning.shot_visual_plan_status_transitions t
            WHERE t.from_status = old_status AND t.to_status = new_status
        ) THEN
            RAISE EXCEPTION
                '非法状态转移 % -> %（未在 shot_visual_plan_status_transitions 登记，ADR-R20）',
                old_status, new_status;
        END IF;
    END IF;

            FOR col IN
                SELECT attname
                FROM pg_attribute
                WHERE attrelid = 'shot_planning.shot_visual_plans'::regclass
                  AND attnum > 0
                  AND NOT attisdropped
            LOOP
                IF col IN ('status', 'entity_version', 'updated_at', 'audit_ref') THEN
                    CONTINUE;
                END IF;
                IF col IN ('locked_fields', 'open_fields') THEN
                    CONTINUE;
                END IF;
                IF (to_jsonb(OLD) ->> col) IS DISTINCT FROM (to_jsonb(NEW) ->> col) THEN
                    RAISE EXCEPTION
                        'shot_planning.shot_visual_plans.% 为不可变内容列，禁止改写（ADR-R27/C2）', col;
                END IF;
            END LOOP;

            RETURN NEW;
        END;
        $function$''',
    # workflow.approval_flows  行标识=entity_id
    'workflow.approval_flows': r'''CREATE OR REPLACE FUNCTION workflow.approval_flows_guard()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
    BEGIN
        -- 受控例外（ADR-R40 §13 D-10.2，v1.4.5 读法裁定后）。
        -- 四条件同时满足才放行；判定逻辑的唯一实现在
        -- project_planning.ownership_repair_allows()，此处只接入、不复制条件。
        IF TG_OP = 'UPDATE' THEN
            IF project_planning.ownership_repair_allows(
                TG_RELID, to_jsonb(OLD), to_jsonb(NEW),
                to_jsonb(OLD) ->> 'entity_id'
            ) THEN
                RETURN NEW;
            END IF;
        END IF;

        RAISE EXCEPTION
            'approval_flows 是 append-only 审批事实，禁止 %（修正须追加新事实）', TG_OP;
    END;
    $function$''',
    # workflow.approval_steps  行标识=entity_id
    'workflow.approval_steps': r'''CREATE OR REPLACE FUNCTION workflow.approval_steps_guard()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
    BEGIN
        -- 受控例外（ADR-R40 §13 D-10.2，v1.4.5 读法裁定后）。
        -- 四条件同时满足才放行；判定逻辑的唯一实现在
        -- project_planning.ownership_repair_allows()，此处只接入、不复制条件。
        IF TG_OP = 'UPDATE' THEN
            IF project_planning.ownership_repair_allows(
                TG_RELID, to_jsonb(OLD), to_jsonb(NEW),
                to_jsonb(OLD) ->> 'entity_id'
            ) THEN
                RETURN NEW;
            END IF;
        END IF;

        RAISE EXCEPTION
            'approval_steps 是 append-only 审批事实，禁止 %（修正须追加新事实）', TG_OP;
    END;
    $function$''',
}

# ---------------------------------------------------------------------------
# 只读映射表：唯一需要提权的地方。
# SECURITY DEFINER 仅用于绕过「业务角色不应拥有映射表 SELECT」这一点；
# 它不接收身份参数、不做授权判定，因此提权不带来决策权。
_MAP_MATCH_FN = """
CREATE OR REPLACE FUNCTION project_planning.ownership_repair_map_match(
    p_table   text,
    p_row_key text,
    p_ticket  text
) RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, project_planning
AS $fn$
    SELECT EXISTS (
        SELECT 1
          FROM project_planning.ownership_repair_map m
         WHERE m.table_name = p_table
           AND m.row_key    = p_row_key
           AND m.ticket_ref = p_ticket
           AND m.applied_at IS NULL
    );
$fn$;
"""

# 四条件判定的唯一实现。**必须是 SECURITY INVOKER（默认，不加 SECURITY DEFINER）**。
# 若写成 SECURITY DEFINER，函数体里的 current_user 会变成函数 owner（迁移执行者/超级用户），
# pg_has_role(current_user,'afs_data_repair','member') 恒为真 ——
# 角色条件形同不存在，例外退化为「任何有表写权限者的万能钥匙」。
# 这不是理论风险：本迁移在验证阶段实测到了（用例 F8 期望 false、实得 true），
# 拆分后由 SECDEF 只承担「读映射表」这一件事，问题消失。
_ALLOWS_FN = """
CREATE OR REPLACE FUNCTION project_planning.ownership_repair_allows(
    p_table   regclass,
    p_old     jsonb,
    p_new     jsonb,
    p_row_key text
) RETURNS boolean
LANGUAGE plpgsql
STABLE
AS $fn$
DECLARE
    v_ticket  text;
    v_changed text[];
BEGIN
    -- 条件 1：会话角色。此处 current_user 是**调用者**（INVOKER），
    -- pg_has_role 读会话真实身份，SET app.xxx 无法伪造。
    IF NOT pg_has_role(current_user, 'afs_data_repair', 'member') THEN
        RETURN false;
    END IF;

    -- 条件 2：工单令牌。
    v_ticket := nullif(current_setting('app.ownership_repair_ticket', true), '');
    IF v_ticket IS NULL THEN
        RETURN false;
    END IF;

    -- 条件 3：变更列必须 ⊆ 归属三列。
    -- 由本函数自算，不接受调用方传入 —— 否则调用方可以谎报「只改了归属列」
    -- 而实际同时改内容列，例外就变成万能钥匙。
    SELECT array_agg(k)
      INTO v_changed
      FROM jsonb_object_keys(p_new) AS k
     WHERE (p_old -> k) IS DISTINCT FROM (p_new -> k);

    IF v_changed IS NULL THEN
        RETURN false;           -- 没有任何变更，不需要例外，也不该用它
    END IF;
    IF NOT (v_changed <@ ARRAY['project_id', 'organization_id', 'tenant_id']) THEN
        RETURN false;
    END IF;

    -- 条件 4：映射行存在、未盖章、工单一致（提权只发生在这个只读查询里）。
    RETURN project_planning.ownership_repair_map_match(
        p_table::text, p_row_key, v_ticket
    );
END;
$fn$;
"""

_MAP_DDL = """
CREATE TABLE IF NOT EXISTS project_planning.ownership_repair_map (
    id                  bigserial PRIMARY KEY,
    table_name          text        NOT NULL,
    row_key             text        NOT NULL,
    old_project_id      text,
    old_organization_id text,
    old_tenant_id       text,
    new_project_id      text,
    new_organization_id text,
    new_tenant_id       text,
    reason              text        NOT NULL,
    ticket_ref          text        NOT NULL,
    applied_at          timestamptz,
    created_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_ownership_repair_map UNIQUE (table_name, row_key, ticket_ref),
    CONSTRAINT ck_ownership_repair_map_reason_len CHECK (length(btrim(reason)) >= 8)
)
"""

_MAP_IDX = """
CREATE INDEX IF NOT EXISTS ix_ownership_repair_map_pending
    ON project_planning.ownership_repair_map (table_name, row_key)
    WHERE applied_at IS NULL
"""

# NOLOGIN：专用角色只作为 SET ROLE 的目标，不直接登录。
_ROLE_DDL = """
DO $do$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'afs_data_repair') THEN
        CREATE ROLE afs_data_repair NOLOGIN;
    END IF;
END
$do$;
"""


def upgrade_sqls() -> list[str]:
    """返回升级语句清单（按序执行）。

    独立成函数是为了让验证脚本执行**与迁移完全相同的那批 SQL**，
    而不是照着迁移再抄一遍 —— 抄一遍就失去了「验证的是迁移本身」这个性质。
    """
    sqls: list[str] = []

    # E0 前置断言：防重复执行 / 防拿错谱系。
    sqls.append(
        "DO $do$ BEGIN "
        "IF to_regprocedure("
        "'project_planning.ownership_repair_allows(regclass,jsonb,jsonb,text)'"
        ") IS NOT NULL OR to_regprocedure("
        "'project_planning.ownership_repair_map_match(text,text,text)'"
        ") IS NOT NULL THEN "
        "RAISE EXCEPTION '受控例外函数已存在：本迁移已执行过或谱系不符'; "
        "END IF; END $do$"
    )
    sqls.append(
        "DO $do$ BEGIN "
        "IF to_regclass('project_planning.ownership_repair_map') IS NOT NULL THEN "
        "RAISE EXCEPTION '映射表已存在：本迁移已执行过或谱系不符'; "
        "END IF; END $do$"
    )

    # E1 专用角色
    sqls.append(_ROLE_DDL)

    # E2 映射表 + 待处理索引
    sqls.append(_MAP_DDL)
    sqls.append(_MAP_IDX)

    # E3 共享判定函数：map_match 只读提权 + allows 四条件判定（INVOKER）
    sqls.append(_MAP_MATCH_FN)
    sqls.append(_ALLOWS_FN)

    # E4 接入：13 个守卫各替换为「原文 + 一段接入」，其余部分逐字不变
    for fqn in GUARD_ORDER:
        sqls.append(GUARD_PATCHED[fqn])

    # E5 撤 PUBLIC：两个函数是 SECURITY DEFINER，PG 又默认给 PUBLIC EXECUTE
    #   （ADR 附录 E #15 把「函数默认 PUBLIC EXECUTE」列为残余风险）。
    #   它们能读映射表并据此放行归属改写，暴露给 PUBLIC 等于把例外交给任何能
    #   执行 SQL 的主体，所以先撤掉，再由「真实表权限」决定授予谁（见下）。
    sqls.append(
        "REVOKE ALL ON FUNCTION "
        "project_planning.ownership_repair_allows(regclass,jsonb,jsonb,text) FROM PUBLIC"
    )
    sqls.append(
        "REVOKE ALL ON FUNCTION "
        "project_planning.ownership_repair_map_match(text,text,text) FROM PUBLIC"
    )
    # E5 之后**刻意不再按角色名单补授**（旧版在此硬编码 ("app_role", "afs_data_repair")）。
    # 名单式授权有三个修不掉的问题，本文件因此放弃它：
    #   ① 把部署配置（POSTGRES_APP_USER 等）写死进迁移，改名后建出无人使用的角色、
    #      或 GRANT 目标不存在导致升级直接失败（CRED-3 / tests/unit/test_db_role_names.py 判定此形态）；
    #   ② 名单无法穷举，漏掉的主体升级后从 P0001 漂移成 42501 —— 同一个非法操作换了
    #      错误码，按 P0001 写的既有处理逻辑静默失效（保护语义漂移）；
    #   ③ 「此刻还没有表权限的角色」被 IF EXISTS 静默跳过，且只授了 allows 没授 map_match。
    #      afs_data_repair 正是这样丢的：它在 d2e3 才获得表权限，于是本步跳过它，
    #      于是 allows 有 EXECUTE 而它内部调用的 map_match 没有 → 守卫一走到就 42501。
    # 授权因此收敛为两条**由事实驱动**的路径，两者的判据都是「它真的能写这些表」：
    #   - E6：任何对受守卫表持 UPDATE/INSERT/DELETE 的主体，在升级时即刻获得（穷举的替代）；
    #   - d2e3：afs_data_repair 在**获得表权限之后**显式补授，时序上名实相符。
    # 已知边界（不假装解决）：E6 只在升级时跑一次；此后新出现的写角色需重跑同一条
    # 收敛语句才能获得 EXECUTE，否则对该表写入会得到 42501。这一点登记在迁移 README，
    # 是「授权随权限变化」的固有代价，不是名单式授权能消除的。

    # E6 按实际表授权收敛，消除「保护语义漂移」：
    #   守卫对**一切写者**生效。若某角色有表 UPDATE 权限却没有本函数 EXECUTE 权限，
    #   它在升级前得到 P0001（守卫拒绝），升级后会得到 42501（权限拒绝）——同一个
    #   非法操作换了错误码，既有调用方按 P0001 做的处理会失效。硬编码角色名无法穷举，
    #   因此改为「谁有权写这些表，谁就有权调用判定函数」。
    sqls.append(
        "DO $do$ DECLARE r record; BEGIN "
        "FOR r IN "
        "SELECT DISTINCT g.grantee::text AS grantee "
        "  FROM information_schema.role_table_grants g "
        " WHERE g.table_schema || '.' || g.table_name IN ("
        + ", ".join("'" + fqn + "'" for fqn in GUARD_ORDER)
        + ") "
        "   AND g.privilege_type IN ('UPDATE', 'INSERT', 'DELETE') "
        "   AND g.grantee <> 'PUBLIC' "
        "LOOP "
        "  EXECUTE format('GRANT USAGE ON SCHEMA project_planning TO %I', r.grantee); "
        "  EXECUTE format("
        "'GRANT EXECUTE ON FUNCTION "
        "project_planning.ownership_repair_allows(regclass,jsonb,jsonb,text) TO %I', r.grantee); "
        "  EXECUTE format("
        "'GRANT EXECUTE ON FUNCTION "
        "project_planning.ownership_repair_map_match(text,text,text) TO %I', r.grantee); "
        "END LOOP; END $do$"
    )
    return sqls


def downgrade_sqls() -> list[str]:
    """返回降级语句清单（按序执行）。"""
    sqls: list[str] = []
    # 先还原 13 个守卫原文（关闭例外通道），再撤函数、表。
    for fqn in GUARD_ORDER:
        sqls.append(GUARD_ORIGINAL[fqn])
    sqls.append(
        "DROP FUNCTION IF EXISTS "
        "project_planning.ownership_repair_allows(regclass,jsonb,jsonb,text)"
    )
    sqls.append(
        "DROP FUNCTION IF EXISTS "
        "project_planning.ownership_repair_map_match(text,text,text)"
    )
    sqls.append("DROP TABLE IF EXISTS project_planning.ownership_repair_map")
    # 角色不删：PostgreSQL 角色是集群级对象，其他库可能已依赖；删角色会引发
    # 不可预期的 REASSIGN 需求。保留一个 NOLOGIN 且无任何授权的空角色是安全的。
    return sqls


def upgrade() -> None:
    for sql in upgrade_sqls():
        op.execute(sql)


def downgrade() -> None:
    for sql in downgrade_sqls():
        op.execute(sql)

