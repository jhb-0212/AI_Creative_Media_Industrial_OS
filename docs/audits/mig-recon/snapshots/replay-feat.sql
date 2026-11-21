--
-- PostgreSQL database dump
--

\restrict q20osEojBg36YyJ6NPl2VYwnKGtGThA5tgpiou8yCCENhEhOaNV9E6azNYJymbm

-- Dumped from database version 16.15 (Debian 16.15-1.pgdg13+2)
-- Dumped by pg_dump version 16.15

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: asset_service; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA asset_service;


--
-- Name: auth; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA auth;


--
-- Name: generation_execution; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA generation_execution;


--
-- Name: outbox; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA outbox;


--
-- Name: project_planning; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA project_planning;


--
-- Name: qc_governance; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA qc_governance;


--
-- Name: shot_planning; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA shot_planning;


--
-- Name: workflow; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA workflow;


--
-- Name: assert_asset_versions_project_assigned(); Type: FUNCTION; Schema: asset_service; Owner: -
--

CREATE FUNCTION asset_service.assert_asset_versions_project_assigned() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF NEW.project_id = 'legacy-unassigned' THEN
                RAISE EXCEPTION
                    '新写入必须带真实 project_id（legacy-unassigned '
                    '仅用于迁移前的历史数据）';
            END IF;
            RETURN NEW;
        END;
        $$;


--
-- Name: assert_qc_reports_project_assigned(); Type: FUNCTION; Schema: asset_service; Owner: -
--

CREATE FUNCTION asset_service.assert_qc_reports_project_assigned() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF NEW.project_id = 'legacy-unassigned' THEN
                RAISE EXCEPTION
                    '新写入必须带真实 project_id（legacy-unassigned '
                    '仅用于迁移前的历史数据）';
            END IF;
            RETURN NEW;
        END;
        $$;


--
-- Name: asset_versions_guard(); Type: FUNCTION; Schema: asset_service; Owner: -
--

CREATE FUNCTION asset_service.asset_versions_guard() RETURNS trigger
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


--
-- Name: delivery_manifests_guard(); Type: FUNCTION; Schema: asset_service; Owner: -
--

CREATE FUNCTION asset_service.delivery_manifests_guard() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
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
$$;


--
-- Name: qc_reports_guard(); Type: FUNCTION; Schema: asset_service; Owner: -
--

CREATE FUNCTION asset_service.qc_reports_guard() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    RAISE EXCEPTION
        'qc_reports 是追加型检测证据，禁止 %（ADR-R11）', TG_OP;
END;
$$;


--
-- Name: generation_tasks_guard(); Type: FUNCTION; Schema: generation_execution; Owner: -
--

CREATE FUNCTION generation_execution.generation_tasks_guard() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
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
        $$;


--
-- Name: production_routes_guard(); Type: FUNCTION; Schema: generation_execution; Owner: -
--

CREATE FUNCTION generation_execution.production_routes_guard() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
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
        $$;


--
-- Name: shot_route_selections_guard(); Type: FUNCTION; Schema: generation_execution; Owner: -
--

CREATE FUNCTION generation_execution.shot_route_selections_guard() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION
                    'generation_execution.shot_route_selections 业务对象不可物理删除';
            END IF;
            RAISE EXCEPTION
                'generation_execution.shot_route_selections 为受理事实（生效路线），禁止改写'
                '（单一路线生成，V205-R14-A9）';
        END;
        $$;


--
-- Name: shot_route_selections_truncate_guard(); Type: FUNCTION; Schema: generation_execution; Owner: -
--

CREATE FUNCTION generation_execution.shot_route_selections_truncate_guard() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            RAISE EXCEPTION
                'generation_execution.shot_route_selections 禁止 TRUNCATE（业务对象不可整体清空）';
        END;
        $$;


--
-- Name: list_active_tenants(); Type: FUNCTION; Schema: outbox; Owner: -
--

CREATE FUNCTION outbox.list_active_tenants() RETURNS TABLE(tenant_id uuid, pending_count bigint, dead_count bigint, oldest_occurred_at timestamp with time zone, oldest_dead_at timestamp with time zone)
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path TO 'outbox', 'pg_catalog'
    AS $$
    SELECT
        e.tenant_id,
        count(*) FILTER (
            WHERE e.published_at IS NULL
              AND e.dead_lettered_at IS NULL
              AND e.next_attempt_at <= now()
        )::bigint AS pending_count,
        count(*) FILTER (
            WHERE e.published_at IS NULL
              AND e.dead_lettered_at IS NOT NULL
        )::bigint AS dead_count,
        min(e.occurred_at) FILTER (
            WHERE e.published_at IS NULL
              AND e.dead_lettered_at IS NULL
              AND e.next_attempt_at <= now()
        ) AS oldest_occurred_at,
        min(e.dead_lettered_at) FILTER (
            WHERE e.published_at IS NULL
              AND e.dead_lettered_at IS NOT NULL
        ) AS oldest_dead_at
    FROM outbox.outbox_events AS e
    WHERE e.published_at IS NULL
    GROUP BY e.tenant_id
$$;


--
-- Name: gate_decisions_guard(); Type: FUNCTION; Schema: qc_governance; Owner: -
--

CREATE FUNCTION qc_governance.gate_decisions_guard() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    RAISE EXCEPTION
        'gate_decisions 是 append-only Gate 裁决事实，禁止 % （裁决不可改写，'
        '修正须新建一条裁决并附新证据）', TG_OP;
END;
$$;


--
-- Name: qc_review_sessions_guard(); Type: FUNCTION; Schema: qc_governance; Owner: -
--

CREATE FUNCTION qc_governance.qc_review_sessions_guard() RETURNS trigger
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
        $$;


--
-- Name: qc_review_sessions_truncate_guard(); Type: FUNCTION; Schema: qc_governance; Owner: -
--

CREATE FUNCTION qc_governance.qc_review_sessions_truncate_guard() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            RAISE EXCEPTION
                'qc_governance.qc_review_sessions 禁止 TRUNCATE（业务对象不可整体清空）';
        END;
        $$;


--
-- Name: constraint_packs_guard(); Type: FUNCTION; Schema: shot_planning; Owner: -
--

CREATE FUNCTION shot_planning.constraint_packs_guard() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
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
        $$;


--
-- Name: shot_specs_guard(); Type: FUNCTION; Schema: shot_planning; Owner: -
--

CREATE FUNCTION shot_planning.shot_specs_guard() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
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
        $$;


--
-- Name: shot_visual_plans_guard(); Type: FUNCTION; Schema: shot_planning; Owner: -
--

CREATE FUNCTION shot_planning.shot_visual_plans_guard() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
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
        $$;


--
-- Name: approval_flows_guard(); Type: FUNCTION; Schema: workflow; Owner: -
--

CREATE FUNCTION workflow.approval_flows_guard() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
    BEGIN
        RAISE EXCEPTION
            'approval_flows 是 append-only 审批事实，禁止 %（修正须追加新事实）', TG_OP;
    END;
    $$;


--
-- Name: approval_steps_guard(); Type: FUNCTION; Schema: workflow; Owner: -
--

CREATE FUNCTION workflow.approval_steps_guard() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
    BEGIN
        RAISE EXCEPTION
            'approval_steps 是 append-only 审批事实，禁止 %（修正须追加新事实）', TG_OP;
    END;
    $$;


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: asset_status_transitions; Type: TABLE; Schema: asset_service; Owner: -
--

CREATE TABLE asset_service.asset_status_transitions (
    from_status character varying(32) NOT NULL,
    to_status character varying(32) NOT NULL
);


--
-- Name: asset_versions; Type: TABLE; Schema: asset_service; Owner: -
--

CREATE TABLE asset_service.asset_versions (
    entity_id character varying(64) NOT NULL,
    entity_type character varying(64) NOT NULL,
    schema_version integer NOT NULL,
    entity_version integer NOT NULL,
    status character varying(32) NOT NULL,
    owner_module character varying(64) NOT NULL,
    tenant_id uuid NOT NULL,
    created_at timestamp with time zone NOT NULL,
    created_by character varying(64) NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    audit_ref character varying(128),
    asset_type character varying(32) NOT NULL,
    sha256 character varying(64) NOT NULL,
    storage_ref jsonb NOT NULL,
    source_asset_version_ids jsonb NOT NULL,
    rights_manifest_ref character varying(64),
    adapter_id character varying(64),
    tool_product character varying(64),
    operator_ref character varying(64),
    version character varying(32),
    organization_id uuid NOT NULL,
    project_id character varying(64) NOT NULL,
    CONSTRAINT ck_asset_versions_status_registered CHECK (((status)::text = ANY ((ARRAY['GENERATED'::character varying, 'TECHNICAL_QC'::character varying, 'HUMAN_REVIEW'::character varying, 'APPROVED'::character varying, 'GOLD'::character varying, 'REJECTED'::character varying, 'QUARANTINED'::character varying, 'REWORK'::character varying, 'SUPERSEDED'::character varying, 'ARCHIVED'::character varying])::text[])))
);


--
-- Name: delivery_manifest_status_transitions; Type: TABLE; Schema: asset_service; Owner: -
--

CREATE TABLE asset_service.delivery_manifest_status_transitions (
    from_status character varying(32) NOT NULL,
    to_status character varying(32) NOT NULL
);


--
-- Name: delivery_manifests; Type: TABLE; Schema: asset_service; Owner: -
--

CREATE TABLE asset_service.delivery_manifests (
    entity_id character varying(64) NOT NULL,
    entity_type character varying(64) NOT NULL,
    schema_version integer NOT NULL,
    entity_version integer NOT NULL,
    status character varying(32) NOT NULL,
    owner_module character varying(64) NOT NULL,
    tenant_id uuid NOT NULL,
    organization_id uuid NOT NULL,
    created_at timestamp with time zone NOT NULL,
    created_by character varying(64) NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    audit_ref character varying(128),
    project_id character varying(64) NOT NULL,
    asset_refs jsonb NOT NULL,
    checksum character varying(64) NOT NULL,
    picture_lock_ref character varying(64) NOT NULL,
    qc_final_ref character varying(64) NOT NULL,
    rebuild_recipe text NOT NULL,
    rights_evidence_refs jsonb NOT NULL,
    spec_ref character varying(64) NOT NULL,
    CONSTRAINT ck_delivery_manifests_status_registered CHECK (((status)::text = ANY ((ARRAY['DRAFT'::character varying, 'VALIDATING'::character varying, 'VALIDATED'::character varying, 'RELEASED'::character varying, 'INVALID'::character varying, 'STALE'::character varying, 'REVOKED'::character varying, 'SUPERSEDED'::character varying])::text[])))
);


--
-- Name: qc_reports; Type: TABLE; Schema: asset_service; Owner: -
--

CREATE TABLE asset_service.qc_reports (
    qc_report_id character varying(64) NOT NULL,
    asset_version_id character varying(64) NOT NULL,
    tenant_id uuid NOT NULL,
    quality_profile_version character varying(64) NOT NULL,
    benchmark_version character varying(64),
    detector_versions jsonb NOT NULL,
    findings jsonb NOT NULL,
    deviation_class character varying(24),
    intent_match_status character varying(24),
    human_decision character varying(32),
    created_by character varying(64) NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    deviation_intent_ref character varying(64),
    organization_id uuid NOT NULL,
    project_id character varying(64) NOT NULL
);


--
-- Name: revoked_tokens; Type: TABLE; Schema: auth; Owner: -
--

CREATE TABLE auth.revoked_tokens (
    jti character varying(64) NOT NULL,
    tenant_id uuid NOT NULL,
    subject_id character varying(64) NOT NULL,
    revoked_at timestamp with time zone DEFAULT now() NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    reason character varying(200),
    revoked_by character varying(64) NOT NULL
);


--
-- Name: generation_task_status_transitions; Type: TABLE; Schema: generation_execution; Owner: -
--

CREATE TABLE generation_execution.generation_task_status_transitions (
    from_status character varying(32) NOT NULL,
    to_status character varying(32) NOT NULL
);


--
-- Name: generation_tasks; Type: TABLE; Schema: generation_execution; Owner: -
--

CREATE TABLE generation_execution.generation_tasks (
    entity_id character varying(64) NOT NULL,
    entity_type character varying(64) NOT NULL,
    schema_version integer NOT NULL,
    entity_version integer NOT NULL,
    status character varying(32) NOT NULL,
    owner_module character varying(64) NOT NULL,
    tenant_id uuid NOT NULL,
    organization_id uuid NOT NULL,
    created_at timestamp with time zone NOT NULL,
    created_by character varying(64) NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    audit_ref character varying(128),
    project_id character varying(64) NOT NULL,
    shot_id character varying(64) NOT NULL,
    route_id character varying(64) NOT NULL,
    shot_spec_ref character varying(64) NOT NULL,
    visual_plan_ref character varying(64) NOT NULL,
    constraint_snapshot_ref character varying(64) NOT NULL,
    budget_reservation_ref character varying(64) NOT NULL,
    quota_reservation_ref character varying(64) NOT NULL,
    dependency_refs jsonb NOT NULL,
    attempt integer NOT NULL,
    retry_of character varying(64),
    result_ref character varying(128),
    started_at timestamp with time zone,
    completed_at timestamp with time zone,
    rights_manifest_ref character varying(64),
    lease_owner character varying(64),
    lease_expires_at timestamp with time zone,
    CONSTRAINT ck_generation_execution_generation_tasks_status_registered CHECK (((status)::text = ANY ((ARRAY['PENDING'::character varying, 'RUNNING'::character varying, 'SUCCEEDED'::character varying, 'FAILED'::character varying])::text[])))
);


--
-- Name: production_route_status_transitions; Type: TABLE; Schema: generation_execution; Owner: -
--

CREATE TABLE generation_execution.production_route_status_transitions (
    from_status character varying(32) NOT NULL,
    to_status character varying(32) NOT NULL
);


--
-- Name: production_routes; Type: TABLE; Schema: generation_execution; Owner: -
--

CREATE TABLE generation_execution.production_routes (
    entity_id character varying(64) NOT NULL,
    entity_type character varying(64) NOT NULL,
    schema_version integer NOT NULL,
    entity_version integer NOT NULL,
    status character varying(32) NOT NULL,
    owner_module character varying(64) NOT NULL,
    tenant_id uuid NOT NULL,
    organization_id uuid NOT NULL,
    created_at timestamp with time zone NOT NULL,
    created_by character varying(64) NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    audit_ref character varying(128),
    route_type character varying(32) NOT NULL,
    supported_control_signals jsonb NOT NULL,
    required_appearance_refs jsonb NOT NULL,
    qc_dimensions jsonb NOT NULL,
    capability_refs jsonb NOT NULL,
    verified_by character varying(64),
    verified_at timestamp with time zone,
    CONSTRAINT ck_generation_execution_production_routes_status_registered CHECK (((status)::text = ANY ((ARRAY['UNVERIFIED'::character varying, 'VERIFIED'::character varying])::text[]))),
    CONSTRAINT ck_generation_route_verified_requires_evidence CHECK ((((status)::text <> 'VERIFIED'::text) OR (jsonb_array_length(capability_refs) > 0)))
);


--
-- Name: shot_route_selections; Type: TABLE; Schema: generation_execution; Owner: -
--

CREATE TABLE generation_execution.shot_route_selections (
    entity_id character varying(64) NOT NULL,
    entity_type character varying(64) NOT NULL,
    schema_version integer NOT NULL,
    entity_version integer NOT NULL,
    status character varying(32) NOT NULL,
    owner_module character varying(64) NOT NULL,
    tenant_id uuid NOT NULL,
    organization_id uuid NOT NULL,
    created_at timestamp with time zone NOT NULL,
    created_by character varying(64) NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    audit_ref character varying(128),
    project_id character varying(64) NOT NULL,
    shot_id character varying(64) NOT NULL,
    route_id character varying(64) NOT NULL,
    CONSTRAINT ck_generation_execution_shot_route_selections_status_registered CHECK (((status)::text = 'ACTIVE'::text))
);


--
-- Name: maintenance_runs; Type: TABLE; Schema: outbox; Owner: -
--

CREATE TABLE outbox.maintenance_runs (
    id bigint NOT NULL,
    started_at timestamp with time zone NOT NULL,
    finished_at timestamp with time zone NOT NULL,
    applied boolean NOT NULL,
    created integer NOT NULL,
    detached integer NOT NULL,
    refused integer NOT NULL,
    status character varying(16) NOT NULL,
    error text
);


--
-- Name: maintenance_runs_id_seq; Type: SEQUENCE; Schema: outbox; Owner: -
--

CREATE SEQUENCE outbox.maintenance_runs_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: maintenance_runs_id_seq; Type: SEQUENCE OWNED BY; Schema: outbox; Owner: -
--

ALTER SEQUENCE outbox.maintenance_runs_id_seq OWNED BY outbox.maintenance_runs.id;


--
-- Name: outbox_events; Type: TABLE; Schema: outbox; Owner: -
--

CREATE TABLE outbox.outbox_events (
    event_id uuid NOT NULL,
    aggregate_type character varying(64) NOT NULL,
    aggregate_id character varying(64) NOT NULL,
    tenant_id uuid NOT NULL,
    event_type character varying(64) NOT NULL,
    payload jsonb NOT NULL,
    occurred_at timestamp with time zone NOT NULL,
    published_at timestamp with time zone,
    attempts integer DEFAULT 0 NOT NULL,
    next_attempt_at timestamp with time zone DEFAULT now() NOT NULL,
    dead_lettered_at timestamp with time zone
)
PARTITION BY RANGE (occurred_at);


--
-- Name: outbox_events_default; Type: TABLE; Schema: outbox; Owner: -
--

CREATE TABLE outbox.outbox_events_default (
    event_id uuid NOT NULL,
    aggregate_type character varying(64) NOT NULL,
    aggregate_id character varying(64) NOT NULL,
    tenant_id uuid NOT NULL,
    event_type character varying(64) NOT NULL,
    payload jsonb NOT NULL,
    occurred_at timestamp with time zone NOT NULL,
    published_at timestamp with time zone,
    attempts integer DEFAULT 0 NOT NULL,
    next_attempt_at timestamp with time zone DEFAULT now() NOT NULL,
    dead_lettered_at timestamp with time zone
);


--
-- Name: outbox_events_y202611; Type: TABLE; Schema: outbox; Owner: -
--

CREATE TABLE outbox.outbox_events_y202611 (
    event_id uuid NOT NULL,
    aggregate_type character varying(64) NOT NULL,
    aggregate_id character varying(64) NOT NULL,
    tenant_id uuid NOT NULL,
    event_type character varying(64) NOT NULL,
    payload jsonb NOT NULL,
    occurred_at timestamp with time zone NOT NULL,
    published_at timestamp with time zone,
    attempts integer DEFAULT 0 NOT NULL,
    next_attempt_at timestamp with time zone DEFAULT now() NOT NULL,
    dead_lettered_at timestamp with time zone
);


--
-- Name: outbox_events_y202612; Type: TABLE; Schema: outbox; Owner: -
--

CREATE TABLE outbox.outbox_events_y202612 (
    event_id uuid NOT NULL,
    aggregate_type character varying(64) NOT NULL,
    aggregate_id character varying(64) NOT NULL,
    tenant_id uuid NOT NULL,
    event_type character varying(64) NOT NULL,
    payload jsonb NOT NULL,
    occurred_at timestamp with time zone NOT NULL,
    published_at timestamp with time zone,
    attempts integer DEFAULT 0 NOT NULL,
    next_attempt_at timestamp with time zone DEFAULT now() NOT NULL,
    dead_lettered_at timestamp with time zone
);


--
-- Name: episodes; Type: TABLE; Schema: project_planning; Owner: -
--

CREATE TABLE project_planning.episodes (
    entity_id character varying(64) NOT NULL,
    entity_type character varying(64) NOT NULL,
    schema_version integer NOT NULL,
    entity_version integer NOT NULL,
    status character varying(32) NOT NULL,
    owner_module character varying(64) NOT NULL,
    created_at timestamp with time zone NOT NULL,
    created_by character varying(64) NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    audit_ref character varying(128),
    project_id character varying(64) NOT NULL,
    episode_ref character varying(64) NOT NULL,
    title character varying(200) NOT NULL,
    tenant_id uuid NOT NULL,
    organization_id uuid NOT NULL
);


--
-- Name: projects; Type: TABLE; Schema: project_planning; Owner: -
--

CREATE TABLE project_planning.projects (
    entity_id character varying(64) NOT NULL,
    entity_type character varying(64) NOT NULL,
    schema_version integer NOT NULL,
    entity_version integer NOT NULL,
    status character varying(32) NOT NULL,
    owner_module character varying(64) NOT NULL,
    created_at timestamp with time zone NOT NULL,
    created_by character varying(64) NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    audit_ref character varying(128),
    project_name character varying(200) NOT NULL,
    project_tier character varying(8) NOT NULL,
    tenant_id uuid NOT NULL,
    organization_id uuid NOT NULL
);


--
-- Name: script_revisions; Type: TABLE; Schema: project_planning; Owner: -
--

CREATE TABLE project_planning.script_revisions (
    entity_id character varying(64) NOT NULL,
    entity_type character varying(64) NOT NULL,
    schema_version integer NOT NULL,
    entity_version integer NOT NULL,
    status character varying(32) NOT NULL,
    owner_module character varying(64) NOT NULL,
    created_at timestamp with time zone NOT NULL,
    created_by character varying(64) NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    audit_ref character varying(128),
    project_id character varying(64) NOT NULL,
    episode_ref character varying(64) NOT NULL,
    title character varying(200) NOT NULL,
    tenant_id uuid NOT NULL,
    requested_by character varying(64),
    organization_id uuid NOT NULL
);


--
-- Name: alembic_version; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.alembic_version (
    version_num character varying(32) NOT NULL
);


--
-- Name: gate_decisions; Type: TABLE; Schema: qc_governance; Owner: -
--

CREATE TABLE qc_governance.gate_decisions (
    entity_id character varying(64) NOT NULL,
    entity_type character varying(64) NOT NULL,
    schema_version integer NOT NULL,
    entity_version integer NOT NULL,
    status character varying(32) NOT NULL,
    owner_module character varying(64) NOT NULL,
    tenant_id uuid NOT NULL,
    organization_id uuid NOT NULL,
    created_at timestamp with time zone NOT NULL,
    created_by character varying(64) NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    audit_ref character varying(128),
    project_id character varying(64) NOT NULL,
    gate_key character varying(32) NOT NULL,
    gate_instance_id character varying(64) NOT NULL,
    decision character varying(24) NOT NULL,
    constraint_snapshot_id character varying(128) NOT NULL,
    ruleset_version integer NOT NULL,
    entry_criteria text NOT NULL,
    required_artifacts text NOT NULL,
    reviewers jsonb NOT NULL,
    reason_code character varying(64) NOT NULL,
    comment text NOT NULL,
    evidence_refs jsonb NOT NULL,
    scope jsonb NOT NULL,
    decided_by character varying(64) NOT NULL,
    decided_by_subject_type character varying(16) NOT NULL,
    decided_at timestamp with time zone NOT NULL,
    CONSTRAINT ck_qc_governance_gate_decisions_approved_needs_evidence CHECK ((((decision)::text <> 'APPROVED'::text) OR (jsonb_array_length(evidence_refs) > 0))),
    CONSTRAINT ck_qc_governance_gate_decisions_decision_registered CHECK (((decision)::text = ANY ((ARRAY['APPROVED'::character varying, 'REJECTED'::character varying, 'RETURNED'::character varying, 'OVERRIDDEN'::character varying])::text[]))),
    CONSTRAINT ck_qc_governance_gate_decisions_no_agent_decider CHECK (((decided_by_subject_type)::text <> 'agent'::text))
);


--
-- Name: qc_review_session_status_transitions; Type: TABLE; Schema: qc_governance; Owner: -
--

CREATE TABLE qc_governance.qc_review_session_status_transitions (
    from_status character varying(32) NOT NULL,
    to_status character varying(32) NOT NULL
);


--
-- Name: qc_review_sessions; Type: TABLE; Schema: qc_governance; Owner: -
--

CREATE TABLE qc_governance.qc_review_sessions (
    entity_id character varying(64) NOT NULL,
    entity_type character varying(64) NOT NULL,
    schema_version integer NOT NULL,
    entity_version integer NOT NULL,
    status character varying(32) NOT NULL,
    owner_module character varying(64) NOT NULL,
    tenant_id uuid NOT NULL,
    organization_id uuid NOT NULL,
    created_at timestamp with time zone NOT NULL,
    created_by character varying(64) NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    audit_ref character varying(128),
    project_id character varying(64) NOT NULL,
    asset_version_id character varying(64) NOT NULL,
    qc_report_ref character varying(64) NOT NULL,
    reference_set jsonb NOT NULL,
    reviewers jsonb NOT NULL,
    findings jsonb NOT NULL,
    decisions jsonb NOT NULL,
    CONSTRAINT ck_qc_governance_qc_review_sessions_findings_not_empty CHECK ((jsonb_array_length(findings) > 0)),
    CONSTRAINT ck_qc_governance_qc_review_sessions_reviewers_not_empty CHECK ((jsonb_array_length(reviewers) > 0)),
    CONSTRAINT ck_qc_governance_qc_review_sessions_status_registered CHECK (((status)::text = ANY ((ARRAY['OPEN'::character varying, 'IN_REVIEW'::character varying, 'DECISION_PENDING'::character varying, 'RESOLVED'::character varying, 'GATE_ELIGIBLE'::character varying, 'CLOSED'::character varying, 'WAIVE_WITH_AUTHORITY'::character varying])::text[])))
);


--
-- Name: constraint_pack_status_transitions; Type: TABLE; Schema: shot_planning; Owner: -
--

CREATE TABLE shot_planning.constraint_pack_status_transitions (
    from_status character varying(32) NOT NULL,
    to_status character varying(32) NOT NULL
);


--
-- Name: constraint_packs; Type: TABLE; Schema: shot_planning; Owner: -
--

CREATE TABLE shot_planning.constraint_packs (
    entity_id character varying(64) NOT NULL,
    entity_type character varying(64) NOT NULL,
    schema_version integer NOT NULL,
    entity_version integer NOT NULL,
    status character varying(32) NOT NULL,
    owner_module character varying(64) NOT NULL,
    tenant_id uuid NOT NULL,
    organization_id uuid NOT NULL,
    created_at timestamp with time zone NOT NULL,
    created_by character varying(64) NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    audit_ref character varying(128),
    project_id character varying(64) NOT NULL,
    scene_id character varying(64),
    shot_ids jsonb NOT NULL,
    shot_group_ids jsonb NOT NULL,
    applicability character varying(16) NOT NULL,
    locked_dimensions jsonb NOT NULL,
    constraints jsonb NOT NULL,
    source_previz_revision character varying(64),
    version integer NOT NULL,
    approved_by character varying(64),
    approved_at timestamp with time zone,
    CONSTRAINT ck_shot_planning_constraint_packs_applicability_registered CHECK (((applicability)::text = ANY ((ARRAY['APPLICABLE'::character varying, 'NOT_APPLICABLE'::character varying])::text[]))),
    CONSTRAINT ck_shot_planning_constraint_packs_status_registered CHECK (((status)::text = ANY ((ARRAY['ACTIVE'::character varying, 'DRAFT'::character varying, 'REVOKED'::character varying, 'STALE'::character varying, 'SUPERSEDED'::character varying])::text[])))
);


--
-- Name: shot_drafts; Type: TABLE; Schema: shot_planning; Owner: -
--

CREATE TABLE shot_planning.shot_drafts (
    entity_id character varying(64) NOT NULL,
    entity_type character varying(64) NOT NULL,
    schema_version integer NOT NULL,
    entity_version integer NOT NULL,
    status character varying(32) NOT NULL,
    owner_module character varying(64) NOT NULL,
    tenant_id uuid NOT NULL,
    organization_id uuid NOT NULL,
    created_at timestamp with time zone NOT NULL,
    created_by character varying(64) NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    audit_ref character varying(128),
    project_id character varying(64) NOT NULL,
    episode_ref character varying(64) NOT NULL,
    scene_ref character varying(64) NOT NULL,
    shot_code character varying(64) NOT NULL,
    intent_summary text NOT NULL,
    shot_description text NOT NULL,
    source character varying(16) NOT NULL,
    CONSTRAINT ck_shot_planning_shot_drafts_source_registered CHECK (((source)::text = ANY ((ARRAY['agent'::character varying, 'human'::character varying])::text[]))),
    CONSTRAINT ck_shot_planning_shot_drafts_status_registered CHECK (((status)::text = ANY ((ARRAY['DRAFT'::character varying, 'CONFIRMED'::character varying, 'SUPERSEDED'::character varying])::text[])))
);


--
-- Name: shot_revisions; Type: TABLE; Schema: shot_planning; Owner: -
--

CREATE TABLE shot_planning.shot_revisions (
    entity_id character varying(64) NOT NULL,
    entity_type character varying(64) NOT NULL,
    schema_version integer NOT NULL,
    entity_version integer NOT NULL,
    status character varying(32) NOT NULL,
    owner_module character varying(64) NOT NULL,
    tenant_id uuid NOT NULL,
    organization_id uuid NOT NULL,
    created_at timestamp with time zone NOT NULL,
    created_by character varying(64) NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    audit_ref character varying(128),
    shot_id character varying(64) NOT NULL,
    project_id character varying(64) NOT NULL,
    episode_ref character varying(64) NOT NULL,
    scene_ref character varying(64) NOT NULL,
    intent_summary text NOT NULL,
    shot_description text NOT NULL,
    revision integer NOT NULL
);


--
-- Name: shot_specs; Type: TABLE; Schema: shot_planning; Owner: -
--

CREATE TABLE shot_planning.shot_specs (
    entity_id character varying(64) NOT NULL,
    entity_type character varying(64) NOT NULL,
    schema_version integer NOT NULL,
    entity_version integer NOT NULL,
    status character varying(32) NOT NULL,
    owner_module character varying(64) NOT NULL,
    tenant_id uuid NOT NULL,
    organization_id uuid NOT NULL,
    created_at timestamp with time zone NOT NULL,
    created_by character varying(64) NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    audit_ref character varying(128),
    shot_revision_id character varying(64) NOT NULL,
    shot_id character varying(64) NOT NULL,
    project_id character varying(64) NOT NULL,
    duration_ms integer NOT NULL,
    aspect_ratio character varying(32) NOT NULL,
    fps character varying(32) NOT NULL,
    coordinate_system text NOT NULL,
    camera_path jsonb NOT NULL,
    subject_paths jsonb NOT NULL,
    shot_size_changes jsonb NOT NULL,
    cut_points_frames jsonb NOT NULL,
    previz_ref character varying(128),
    control_signal_refs jsonb NOT NULL,
    locked_dimensions jsonb NOT NULL,
    locked_by character varying(64),
    locked_at timestamp with time zone,
    lock_rationale text,
    CONSTRAINT ck_shot_planning_shot_specs_status_registered CHECK (((status)::text = 'ACTIVE'::text))
);


--
-- Name: shot_visual_plan_status_transitions; Type: TABLE; Schema: shot_planning; Owner: -
--

CREATE TABLE shot_planning.shot_visual_plan_status_transitions (
    from_status character varying(32) NOT NULL,
    to_status character varying(32) NOT NULL
);


--
-- Name: shot_visual_plans; Type: TABLE; Schema: shot_planning; Owner: -
--

CREATE TABLE shot_planning.shot_visual_plans (
    entity_id character varying(64) NOT NULL,
    entity_type character varying(64) NOT NULL,
    schema_version integer NOT NULL,
    entity_version integer NOT NULL,
    status character varying(32) NOT NULL,
    owner_module character varying(64) NOT NULL,
    tenant_id uuid NOT NULL,
    organization_id uuid NOT NULL,
    created_at timestamp with time zone NOT NULL,
    created_by character varying(64) NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    audit_ref character varying(128),
    shot_id character varying(64) NOT NULL,
    project_id character varying(64) NOT NULL,
    intent_summary text NOT NULL,
    camera_language character varying(128) NOT NULL,
    shot_size character varying(32) NOT NULL,
    lens_range character varying(64) NOT NULL,
    motion_intent text NOT NULL,
    lighting_intent text NOT NULL,
    locked_fields jsonb NOT NULL,
    open_fields jsonb NOT NULL,
    revision integer NOT NULL,
    CONSTRAINT ck_shot_planning_shot_visual_plans_status_registered CHECK (((status)::text = ANY ((ARRAY['APPROVED'::character varying, 'DRAFT'::character varying, 'LOCKED'::character varying, 'PARTIALLY_LOCKED'::character varying, 'REJECTED'::character varying, 'REVIEW'::character varying])::text[])))
);


--
-- Name: approval_flows; Type: TABLE; Schema: workflow; Owner: -
--

CREATE TABLE workflow.approval_flows (
    entity_id character varying(64) NOT NULL,
    entity_type character varying(64) NOT NULL,
    schema_version integer NOT NULL,
    entity_version integer NOT NULL,
    status character varying(32) NOT NULL,
    owner_module character varying(64) NOT NULL,
    tenant_id uuid NOT NULL,
    organization_id uuid NOT NULL,
    created_at timestamp with time zone NOT NULL,
    created_by character varying(64) NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    audit_ref character varying(128),
    action_key character varying(96) NOT NULL,
    resource_type character varying(96) NOT NULL,
    resource_id character varying(96) NOT NULL,
    project_id character varying(64) NOT NULL,
    mode character varying(8) NOT NULL,
    groups jsonb NOT NULL,
    ruleset_version integer NOT NULL,
    CONSTRAINT ck_workflow_approval_flows_mode_registered CHECK (((mode)::text = ANY ((ARRAY['ALL'::character varying, 'ANY'::character varying])::text[])))
);


--
-- Name: approval_steps; Type: TABLE; Schema: workflow; Owner: -
--

CREATE TABLE workflow.approval_steps (
    entity_id character varying(64) NOT NULL,
    entity_type character varying(64) NOT NULL,
    schema_version integer NOT NULL,
    entity_version integer NOT NULL,
    status character varying(32) NOT NULL,
    owner_module character varying(64) NOT NULL,
    tenant_id uuid NOT NULL,
    organization_id uuid NOT NULL,
    created_at timestamp with time zone NOT NULL,
    created_by character varying(64) NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    audit_ref character varying(128),
    ticket_id character varying(64) NOT NULL,
    group_name character varying(64) NOT NULL,
    voter_subject_id character varying(64) NOT NULL,
    voter_subject_type character varying(16) NOT NULL,
    decision character varying(16) NOT NULL,
    reason text NOT NULL,
    decided_at timestamp with time zone NOT NULL,
    project_id character varying(64) NOT NULL,
    CONSTRAINT ck_workflow_approval_steps_decision_registered CHECK (((decision)::text = ANY ((ARRAY['APPROVE'::character varying, 'REJECT'::character varying])::text[]))),
    CONSTRAINT ck_workflow_approval_steps_no_agent_voter CHECK (((voter_subject_type)::text <> 'agent'::text)),
    CONSTRAINT ck_workflow_approval_steps_reject_needs_reason CHECK ((((decision)::text <> 'REJECT'::text) OR (length(btrim(reason)) > 0)))
);


--
-- Name: approval_tickets; Type: TABLE; Schema: workflow; Owner: -
--

CREATE TABLE workflow.approval_tickets (
    entity_id character varying(64) NOT NULL,
    entity_type character varying(64) NOT NULL,
    schema_version integer NOT NULL,
    entity_version integer NOT NULL,
    status character varying(32) NOT NULL,
    owner_module character varying(64) NOT NULL,
    tenant_id uuid NOT NULL,
    organization_id uuid NOT NULL,
    created_at timestamp with time zone NOT NULL,
    created_by character varying(64) NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    audit_ref character varying(128),
    flow_id character varying(64) NOT NULL,
    project_id character varying(64) NOT NULL,
    version integer NOT NULL,
    completed_at timestamp with time zone,
    CONSTRAINT ck_workflow_approval_tickets_status_registered CHECK (((status)::text = ANY ((ARRAY['PENDING'::character varying, 'APPROVED'::character varying, 'REJECTED'::character varying, 'CANCELLED'::character varying])::text[]))),
    CONSTRAINT ck_workflow_approval_tickets_terminal_has_time CHECK ((((status)::text = 'PENDING'::text) OR (completed_at IS NOT NULL)))
);


--
-- Name: outbox_events_default; Type: TABLE ATTACH; Schema: outbox; Owner: -
--

ALTER TABLE ONLY outbox.outbox_events ATTACH PARTITION outbox.outbox_events_default DEFAULT;


--
-- Name: outbox_events_y202611; Type: TABLE ATTACH; Schema: outbox; Owner: -
--

ALTER TABLE ONLY outbox.outbox_events ATTACH PARTITION outbox.outbox_events_y202611 FOR VALUES FROM ('2026-11-01 00:00:00+00') TO ('2026-12-01 00:00:00+00');


--
-- Name: outbox_events_y202612; Type: TABLE ATTACH; Schema: outbox; Owner: -
--

ALTER TABLE ONLY outbox.outbox_events ATTACH PARTITION outbox.outbox_events_y202612 FOR VALUES FROM ('2026-12-01 00:00:00+00') TO ('2027-01-01 00:00:00+00');


--
-- Name: maintenance_runs id; Type: DEFAULT; Schema: outbox; Owner: -
--

ALTER TABLE ONLY outbox.maintenance_runs ALTER COLUMN id SET DEFAULT nextval('outbox.maintenance_runs_id_seq'::regclass);


--
-- Name: asset_status_transitions asset_status_transitions_pkey; Type: CONSTRAINT; Schema: asset_service; Owner: -
--

ALTER TABLE ONLY asset_service.asset_status_transitions
    ADD CONSTRAINT asset_status_transitions_pkey PRIMARY KEY (from_status, to_status);


--
-- Name: asset_versions asset_versions_pkey; Type: CONSTRAINT; Schema: asset_service; Owner: -
--

ALTER TABLE ONLY asset_service.asset_versions
    ADD CONSTRAINT asset_versions_pkey PRIMARY KEY (entity_id);


--
-- Name: delivery_manifest_status_transitions delivery_manifest_status_transitions_pkey; Type: CONSTRAINT; Schema: asset_service; Owner: -
--

ALTER TABLE ONLY asset_service.delivery_manifest_status_transitions
    ADD CONSTRAINT delivery_manifest_status_transitions_pkey PRIMARY KEY (from_status, to_status);


--
-- Name: delivery_manifests delivery_manifests_pkey; Type: CONSTRAINT; Schema: asset_service; Owner: -
--

ALTER TABLE ONLY asset_service.delivery_manifests
    ADD CONSTRAINT delivery_manifests_pkey PRIMARY KEY (entity_id);


--
-- Name: qc_reports qc_reports_pkey; Type: CONSTRAINT; Schema: asset_service; Owner: -
--

ALTER TABLE ONLY asset_service.qc_reports
    ADD CONSTRAINT qc_reports_pkey PRIMARY KEY (qc_report_id);


--
-- Name: revoked_tokens revoked_tokens_pkey; Type: CONSTRAINT; Schema: auth; Owner: -
--

ALTER TABLE ONLY auth.revoked_tokens
    ADD CONSTRAINT revoked_tokens_pkey PRIMARY KEY (jti);


--
-- Name: generation_task_status_transitions generation_task_status_transitions_pkey; Type: CONSTRAINT; Schema: generation_execution; Owner: -
--

ALTER TABLE ONLY generation_execution.generation_task_status_transitions
    ADD CONSTRAINT generation_task_status_transitions_pkey PRIMARY KEY (from_status, to_status);


--
-- Name: generation_tasks generation_tasks_pkey; Type: CONSTRAINT; Schema: generation_execution; Owner: -
--

ALTER TABLE ONLY generation_execution.generation_tasks
    ADD CONSTRAINT generation_tasks_pkey PRIMARY KEY (entity_id);


--
-- Name: production_route_status_transitions production_route_status_transitions_pkey; Type: CONSTRAINT; Schema: generation_execution; Owner: -
--

ALTER TABLE ONLY generation_execution.production_route_status_transitions
    ADD CONSTRAINT production_route_status_transitions_pkey PRIMARY KEY (from_status, to_status);


--
-- Name: production_routes production_routes_pkey; Type: CONSTRAINT; Schema: generation_execution; Owner: -
--

ALTER TABLE ONLY generation_execution.production_routes
    ADD CONSTRAINT production_routes_pkey PRIMARY KEY (entity_id);


--
-- Name: shot_route_selections shot_route_selections_pkey; Type: CONSTRAINT; Schema: generation_execution; Owner: -
--

ALTER TABLE ONLY generation_execution.shot_route_selections
    ADD CONSTRAINT shot_route_selections_pkey PRIMARY KEY (entity_id);


--
-- Name: shot_route_selections ux_shot_route_selection_effective; Type: CONSTRAINT; Schema: generation_execution; Owner: -
--

ALTER TABLE ONLY generation_execution.shot_route_selections
    ADD CONSTRAINT ux_shot_route_selection_effective UNIQUE (tenant_id, project_id, shot_id);


--
-- Name: maintenance_runs maintenance_runs_pkey; Type: CONSTRAINT; Schema: outbox; Owner: -
--

ALTER TABLE ONLY outbox.maintenance_runs
    ADD CONSTRAINT maintenance_runs_pkey PRIMARY KEY (id);


--
-- Name: outbox_events outbox_events_pkey; Type: CONSTRAINT; Schema: outbox; Owner: -
--

ALTER TABLE ONLY outbox.outbox_events
    ADD CONSTRAINT outbox_events_pkey PRIMARY KEY (event_id, occurred_at);


--
-- Name: outbox_events_default outbox_events_default_pkey; Type: CONSTRAINT; Schema: outbox; Owner: -
--

ALTER TABLE ONLY outbox.outbox_events_default
    ADD CONSTRAINT outbox_events_default_pkey PRIMARY KEY (event_id, occurred_at);


--
-- Name: outbox_events_y202611 outbox_events_y202611_pkey; Type: CONSTRAINT; Schema: outbox; Owner: -
--

ALTER TABLE ONLY outbox.outbox_events_y202611
    ADD CONSTRAINT outbox_events_y202611_pkey PRIMARY KEY (event_id, occurred_at);


--
-- Name: outbox_events_y202612 outbox_events_y202612_pkey; Type: CONSTRAINT; Schema: outbox; Owner: -
--

ALTER TABLE ONLY outbox.outbox_events_y202612
    ADD CONSTRAINT outbox_events_y202612_pkey PRIMARY KEY (event_id, occurred_at);


--
-- Name: episodes episodes_pkey; Type: CONSTRAINT; Schema: project_planning; Owner: -
--

ALTER TABLE ONLY project_planning.episodes
    ADD CONSTRAINT episodes_pkey PRIMARY KEY (entity_id);


--
-- Name: projects projects_pkey; Type: CONSTRAINT; Schema: project_planning; Owner: -
--

ALTER TABLE ONLY project_planning.projects
    ADD CONSTRAINT projects_pkey PRIMARY KEY (entity_id);


--
-- Name: script_revisions script_revisions_pkey; Type: CONSTRAINT; Schema: project_planning; Owner: -
--

ALTER TABLE ONLY project_planning.script_revisions
    ADD CONSTRAINT script_revisions_pkey PRIMARY KEY (entity_id);


--
-- Name: episodes ux_episode_ref_per_project; Type: CONSTRAINT; Schema: project_planning; Owner: -
--

ALTER TABLE ONLY project_planning.episodes
    ADD CONSTRAINT ux_episode_ref_per_project UNIQUE (project_id, episode_ref);


--
-- Name: alembic_version alembic_version_pkc; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.alembic_version
    ADD CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num);


--
-- Name: gate_decisions gate_decisions_pkey; Type: CONSTRAINT; Schema: qc_governance; Owner: -
--

ALTER TABLE ONLY qc_governance.gate_decisions
    ADD CONSTRAINT gate_decisions_pkey PRIMARY KEY (entity_id);


--
-- Name: qc_review_session_status_transitions qc_review_session_status_transitions_pkey; Type: CONSTRAINT; Schema: qc_governance; Owner: -
--

ALTER TABLE ONLY qc_governance.qc_review_session_status_transitions
    ADD CONSTRAINT qc_review_session_status_transitions_pkey PRIMARY KEY (from_status, to_status);


--
-- Name: qc_review_sessions qc_review_sessions_pkey; Type: CONSTRAINT; Schema: qc_governance; Owner: -
--

ALTER TABLE ONLY qc_governance.qc_review_sessions
    ADD CONSTRAINT qc_review_sessions_pkey PRIMARY KEY (entity_id);


--
-- Name: constraint_pack_status_transitions constraint_pack_status_transitions_pkey; Type: CONSTRAINT; Schema: shot_planning; Owner: -
--

ALTER TABLE ONLY shot_planning.constraint_pack_status_transitions
    ADD CONSTRAINT constraint_pack_status_transitions_pkey PRIMARY KEY (from_status, to_status);


--
-- Name: constraint_packs constraint_packs_pkey; Type: CONSTRAINT; Schema: shot_planning; Owner: -
--

ALTER TABLE ONLY shot_planning.constraint_packs
    ADD CONSTRAINT constraint_packs_pkey PRIMARY KEY (entity_id);


--
-- Name: shot_drafts shot_drafts_pkey; Type: CONSTRAINT; Schema: shot_planning; Owner: -
--

ALTER TABLE ONLY shot_planning.shot_drafts
    ADD CONSTRAINT shot_drafts_pkey PRIMARY KEY (entity_id);


--
-- Name: shot_revisions shot_revisions_pkey; Type: CONSTRAINT; Schema: shot_planning; Owner: -
--

ALTER TABLE ONLY shot_planning.shot_revisions
    ADD CONSTRAINT shot_revisions_pkey PRIMARY KEY (entity_id);


--
-- Name: shot_specs shot_specs_pkey; Type: CONSTRAINT; Schema: shot_planning; Owner: -
--

ALTER TABLE ONLY shot_planning.shot_specs
    ADD CONSTRAINT shot_specs_pkey PRIMARY KEY (entity_id);


--
-- Name: shot_visual_plan_status_transitions shot_visual_plan_status_transitions_pkey; Type: CONSTRAINT; Schema: shot_planning; Owner: -
--

ALTER TABLE ONLY shot_planning.shot_visual_plan_status_transitions
    ADD CONSTRAINT shot_visual_plan_status_transitions_pkey PRIMARY KEY (from_status, to_status);


--
-- Name: shot_visual_plans shot_visual_plans_pkey; Type: CONSTRAINT; Schema: shot_planning; Owner: -
--

ALTER TABLE ONLY shot_planning.shot_visual_plans
    ADD CONSTRAINT shot_visual_plans_pkey PRIMARY KEY (entity_id);


--
-- Name: approval_flows approval_flows_pkey; Type: CONSTRAINT; Schema: workflow; Owner: -
--

ALTER TABLE ONLY workflow.approval_flows
    ADD CONSTRAINT approval_flows_pkey PRIMARY KEY (entity_id);


--
-- Name: approval_steps approval_steps_pkey; Type: CONSTRAINT; Schema: workflow; Owner: -
--

ALTER TABLE ONLY workflow.approval_steps
    ADD CONSTRAINT approval_steps_pkey PRIMARY KEY (entity_id);


--
-- Name: approval_tickets approval_tickets_pkey; Type: CONSTRAINT; Schema: workflow; Owner: -
--

ALTER TABLE ONLY workflow.approval_tickets
    ADD CONSTRAINT approval_tickets_pkey PRIMARY KEY (entity_id);


--
-- Name: approval_steps uq_workflow_approval_steps_one_vote_per_group; Type: CONSTRAINT; Schema: workflow; Owner: -
--

ALTER TABLE ONLY workflow.approval_steps
    ADD CONSTRAINT uq_workflow_approval_steps_one_vote_per_group UNIQUE (ticket_id, group_name, voter_subject_id);


--
-- Name: ix_asset_service_asset_versions_organization_id; Type: INDEX; Schema: asset_service; Owner: -
--

CREATE INDEX ix_asset_service_asset_versions_organization_id ON asset_service.asset_versions USING btree (organization_id);


--
-- Name: ix_asset_service_asset_versions_project_id; Type: INDEX; Schema: asset_service; Owner: -
--

CREATE INDEX ix_asset_service_asset_versions_project_id ON asset_service.asset_versions USING btree (project_id);


--
-- Name: ix_asset_service_asset_versions_tenant_id; Type: INDEX; Schema: asset_service; Owner: -
--

CREATE INDEX ix_asset_service_asset_versions_tenant_id ON asset_service.asset_versions USING btree (tenant_id);


--
-- Name: ix_asset_service_asset_versions_type_sha256; Type: INDEX; Schema: asset_service; Owner: -
--

CREATE INDEX ix_asset_service_asset_versions_type_sha256 ON asset_service.asset_versions USING btree (asset_type, sha256);


--
-- Name: ix_asset_service_delivery_manifests_organization_id; Type: INDEX; Schema: asset_service; Owner: -
--

CREATE INDEX ix_asset_service_delivery_manifests_organization_id ON asset_service.delivery_manifests USING btree (organization_id);


--
-- Name: ix_asset_service_delivery_manifests_project; Type: INDEX; Schema: asset_service; Owner: -
--

CREATE INDEX ix_asset_service_delivery_manifests_project ON asset_service.delivery_manifests USING btree (project_id);


--
-- Name: ix_asset_service_delivery_manifests_tenant_id; Type: INDEX; Schema: asset_service; Owner: -
--

CREATE INDEX ix_asset_service_delivery_manifests_tenant_id ON asset_service.delivery_manifests USING btree (tenant_id);


--
-- Name: ix_asset_service_qc_reports_asset_version_id; Type: INDEX; Schema: asset_service; Owner: -
--

CREATE INDEX ix_asset_service_qc_reports_asset_version_id ON asset_service.qc_reports USING btree (asset_version_id);


--
-- Name: ix_asset_service_qc_reports_organization_id; Type: INDEX; Schema: asset_service; Owner: -
--

CREATE INDEX ix_asset_service_qc_reports_organization_id ON asset_service.qc_reports USING btree (organization_id);


--
-- Name: ix_asset_service_qc_reports_project_id; Type: INDEX; Schema: asset_service; Owner: -
--

CREATE INDEX ix_asset_service_qc_reports_project_id ON asset_service.qc_reports USING btree (project_id);


--
-- Name: ix_asset_service_qc_reports_tenant_id; Type: INDEX; Schema: asset_service; Owner: -
--

CREATE INDEX ix_asset_service_qc_reports_tenant_id ON asset_service.qc_reports USING btree (tenant_id);


--
-- Name: ix_auth_revoked_tokens_expires_at; Type: INDEX; Schema: auth; Owner: -
--

CREATE INDEX ix_auth_revoked_tokens_expires_at ON auth.revoked_tokens USING btree (expires_at);


--
-- Name: ix_generation_execution_generation_tasks_organization_id; Type: INDEX; Schema: generation_execution; Owner: -
--

CREATE INDEX ix_generation_execution_generation_tasks_organization_id ON generation_execution.generation_tasks USING btree (organization_id);


--
-- Name: ix_generation_execution_generation_tasks_project_id; Type: INDEX; Schema: generation_execution; Owner: -
--

CREATE INDEX ix_generation_execution_generation_tasks_project_id ON generation_execution.generation_tasks USING btree (project_id);


--
-- Name: ix_generation_execution_generation_tasks_shot_id; Type: INDEX; Schema: generation_execution; Owner: -
--

CREATE INDEX ix_generation_execution_generation_tasks_shot_id ON generation_execution.generation_tasks USING btree (shot_id);


--
-- Name: ix_generation_execution_generation_tasks_tenant_id; Type: INDEX; Schema: generation_execution; Owner: -
--

CREATE INDEX ix_generation_execution_generation_tasks_tenant_id ON generation_execution.generation_tasks USING btree (tenant_id);


--
-- Name: ix_generation_execution_production_routes_organization_id; Type: INDEX; Schema: generation_execution; Owner: -
--

CREATE INDEX ix_generation_execution_production_routes_organization_id ON generation_execution.production_routes USING btree (organization_id);


--
-- Name: ix_generation_execution_production_routes_tenant_id; Type: INDEX; Schema: generation_execution; Owner: -
--

CREATE INDEX ix_generation_execution_production_routes_tenant_id ON generation_execution.production_routes USING btree (tenant_id);


--
-- Name: ix_generation_execution_shot_route_selections_organization_id; Type: INDEX; Schema: generation_execution; Owner: -
--

CREATE INDEX ix_generation_execution_shot_route_selections_organization_id ON generation_execution.shot_route_selections USING btree (organization_id);


--
-- Name: ix_generation_execution_shot_route_selections_project_id; Type: INDEX; Schema: generation_execution; Owner: -
--

CREATE INDEX ix_generation_execution_shot_route_selections_project_id ON generation_execution.shot_route_selections USING btree (project_id);


--
-- Name: ix_generation_execution_shot_route_selections_tenant_id; Type: INDEX; Schema: generation_execution; Owner: -
--

CREATE INDEX ix_generation_execution_shot_route_selections_tenant_id ON generation_execution.shot_route_selections USING btree (tenant_id);


--
-- Name: ix_generation_tasks_lease_expiry; Type: INDEX; Schema: generation_execution; Owner: -
--

CREATE INDEX ix_generation_tasks_lease_expiry ON generation_execution.generation_tasks USING btree (lease_expires_at) WHERE ((status)::text = 'RUNNING'::text);


--
-- Name: ix_outbox_aggregate; Type: INDEX; Schema: outbox; Owner: -
--

CREATE INDEX ix_outbox_aggregate ON ONLY outbox.outbox_events USING btree (aggregate_id);


--
-- Name: ix_outbox_maintenance_runs_started; Type: INDEX; Schema: outbox; Owner: -
--

CREATE INDEX ix_outbox_maintenance_runs_started ON outbox.maintenance_runs USING btree (started_at);


--
-- Name: ix_outbox_published; Type: INDEX; Schema: outbox; Owner: -
--

CREATE INDEX ix_outbox_published ON ONLY outbox.outbox_events USING btree (published_at);


--
-- Name: ix_outbox_tenant; Type: INDEX; Schema: outbox; Owner: -
--

CREATE INDEX ix_outbox_tenant ON ONLY outbox.outbox_events USING btree (tenant_id);


--
-- Name: outbox_events_default_aggregate_id_idx; Type: INDEX; Schema: outbox; Owner: -
--

CREATE INDEX outbox_events_default_aggregate_id_idx ON outbox.outbox_events_default USING btree (aggregate_id);


--
-- Name: outbox_events_default_published_at_idx; Type: INDEX; Schema: outbox; Owner: -
--

CREATE INDEX outbox_events_default_published_at_idx ON outbox.outbox_events_default USING btree (published_at);


--
-- Name: outbox_events_default_tenant_id_idx; Type: INDEX; Schema: outbox; Owner: -
--

CREATE INDEX outbox_events_default_tenant_id_idx ON outbox.outbox_events_default USING btree (tenant_id);


--
-- Name: outbox_events_y202611_aggregate_id_idx; Type: INDEX; Schema: outbox; Owner: -
--

CREATE INDEX outbox_events_y202611_aggregate_id_idx ON outbox.outbox_events_y202611 USING btree (aggregate_id);


--
-- Name: outbox_events_y202611_published_at_idx; Type: INDEX; Schema: outbox; Owner: -
--

CREATE INDEX outbox_events_y202611_published_at_idx ON outbox.outbox_events_y202611 USING btree (published_at);


--
-- Name: outbox_events_y202611_tenant_id_idx; Type: INDEX; Schema: outbox; Owner: -
--

CREATE INDEX outbox_events_y202611_tenant_id_idx ON outbox.outbox_events_y202611 USING btree (tenant_id);


--
-- Name: outbox_events_y202612_aggregate_id_idx; Type: INDEX; Schema: outbox; Owner: -
--

CREATE INDEX outbox_events_y202612_aggregate_id_idx ON outbox.outbox_events_y202612 USING btree (aggregate_id);


--
-- Name: outbox_events_y202612_published_at_idx; Type: INDEX; Schema: outbox; Owner: -
--

CREATE INDEX outbox_events_y202612_published_at_idx ON outbox.outbox_events_y202612 USING btree (published_at);


--
-- Name: outbox_events_y202612_tenant_id_idx; Type: INDEX; Schema: outbox; Owner: -
--

CREATE INDEX outbox_events_y202612_tenant_id_idx ON outbox.outbox_events_y202612 USING btree (tenant_id);


--
-- Name: ix_project_planning_episodes_organization_id; Type: INDEX; Schema: project_planning; Owner: -
--

CREATE INDEX ix_project_planning_episodes_organization_id ON project_planning.episodes USING btree (organization_id);


--
-- Name: ix_project_planning_episodes_project_id; Type: INDEX; Schema: project_planning; Owner: -
--

CREATE INDEX ix_project_planning_episodes_project_id ON project_planning.episodes USING btree (project_id);


--
-- Name: ix_project_planning_episodes_tenant_id; Type: INDEX; Schema: project_planning; Owner: -
--

CREATE INDEX ix_project_planning_episodes_tenant_id ON project_planning.episodes USING btree (tenant_id);


--
-- Name: ix_project_planning_projects_organization_id; Type: INDEX; Schema: project_planning; Owner: -
--

CREATE INDEX ix_project_planning_projects_organization_id ON project_planning.projects USING btree (organization_id);


--
-- Name: ix_project_planning_projects_tenant_id; Type: INDEX; Schema: project_planning; Owner: -
--

CREATE INDEX ix_project_planning_projects_tenant_id ON project_planning.projects USING btree (tenant_id);


--
-- Name: ix_project_planning_script_revisions_organization_id; Type: INDEX; Schema: project_planning; Owner: -
--

CREATE INDEX ix_project_planning_script_revisions_organization_id ON project_planning.script_revisions USING btree (organization_id);


--
-- Name: ix_project_planning_script_revisions_project_id; Type: INDEX; Schema: project_planning; Owner: -
--

CREATE INDEX ix_project_planning_script_revisions_project_id ON project_planning.script_revisions USING btree (project_id);


--
-- Name: ix_project_planning_script_revisions_tenant_id; Type: INDEX; Schema: project_planning; Owner: -
--

CREATE INDEX ix_project_planning_script_revisions_tenant_id ON project_planning.script_revisions USING btree (tenant_id);


--
-- Name: ux_script_active_version; Type: INDEX; Schema: project_planning; Owner: -
--

CREATE UNIQUE INDEX ux_script_active_version ON project_planning.script_revisions USING btree (project_id, episode_ref) WHERE ((status)::text <> 'SUPERSEDED'::text);


--
-- Name: ix_qc_governance_gate_decisions_instance; Type: INDEX; Schema: qc_governance; Owner: -
--

CREATE INDEX ix_qc_governance_gate_decisions_instance ON qc_governance.gate_decisions USING btree (gate_instance_id);


--
-- Name: ix_qc_governance_gate_decisions_organization_id; Type: INDEX; Schema: qc_governance; Owner: -
--

CREATE INDEX ix_qc_governance_gate_decisions_organization_id ON qc_governance.gate_decisions USING btree (organization_id);


--
-- Name: ix_qc_governance_gate_decisions_project; Type: INDEX; Schema: qc_governance; Owner: -
--

CREATE INDEX ix_qc_governance_gate_decisions_project ON qc_governance.gate_decisions USING btree (project_id);


--
-- Name: ix_qc_governance_gate_decisions_tenant_id; Type: INDEX; Schema: qc_governance; Owner: -
--

CREATE INDEX ix_qc_governance_gate_decisions_tenant_id ON qc_governance.gate_decisions USING btree (tenant_id);


--
-- Name: ix_qc_governance_qc_review_sessions_asset_version; Type: INDEX; Schema: qc_governance; Owner: -
--

CREATE INDEX ix_qc_governance_qc_review_sessions_asset_version ON qc_governance.qc_review_sessions USING btree (asset_version_id);


--
-- Name: ix_qc_governance_qc_review_sessions_organization_id; Type: INDEX; Schema: qc_governance; Owner: -
--

CREATE INDEX ix_qc_governance_qc_review_sessions_organization_id ON qc_governance.qc_review_sessions USING btree (organization_id);


--
-- Name: ix_qc_governance_qc_review_sessions_project; Type: INDEX; Schema: qc_governance; Owner: -
--

CREATE INDEX ix_qc_governance_qc_review_sessions_project ON qc_governance.qc_review_sessions USING btree (project_id);


--
-- Name: ix_qc_governance_qc_review_sessions_tenant_id; Type: INDEX; Schema: qc_governance; Owner: -
--

CREATE INDEX ix_qc_governance_qc_review_sessions_tenant_id ON qc_governance.qc_review_sessions USING btree (tenant_id);


--
-- Name: uq_qc_governance_gate_decisions_idempotency; Type: INDEX; Schema: qc_governance; Owner: -
--

CREATE UNIQUE INDEX uq_qc_governance_gate_decisions_idempotency ON qc_governance.gate_decisions USING btree (gate_instance_id, constraint_snapshot_id, ruleset_version, decision);


--
-- Name: uq_qc_governance_qc_review_sessions_report; Type: INDEX; Schema: qc_governance; Owner: -
--

CREATE UNIQUE INDEX uq_qc_governance_qc_review_sessions_report ON qc_governance.qc_review_sessions USING btree (tenant_id, project_id, qc_report_ref);


--
-- Name: ix_shot_planning_constraint_packs_organization_id; Type: INDEX; Schema: shot_planning; Owner: -
--

CREATE INDEX ix_shot_planning_constraint_packs_organization_id ON shot_planning.constraint_packs USING btree (organization_id);


--
-- Name: ix_shot_planning_constraint_packs_project_id; Type: INDEX; Schema: shot_planning; Owner: -
--

CREATE INDEX ix_shot_planning_constraint_packs_project_id ON shot_planning.constraint_packs USING btree (project_id);


--
-- Name: ix_shot_planning_constraint_packs_tenant_id; Type: INDEX; Schema: shot_planning; Owner: -
--

CREATE INDEX ix_shot_planning_constraint_packs_tenant_id ON shot_planning.constraint_packs USING btree (tenant_id);


--
-- Name: ix_shot_planning_shot_drafts_organization_id; Type: INDEX; Schema: shot_planning; Owner: -
--

CREATE INDEX ix_shot_planning_shot_drafts_organization_id ON shot_planning.shot_drafts USING btree (organization_id);


--
-- Name: ix_shot_planning_shot_drafts_project_id; Type: INDEX; Schema: shot_planning; Owner: -
--

CREATE INDEX ix_shot_planning_shot_drafts_project_id ON shot_planning.shot_drafts USING btree (project_id);


--
-- Name: ix_shot_planning_shot_drafts_tenant_id; Type: INDEX; Schema: shot_planning; Owner: -
--

CREATE INDEX ix_shot_planning_shot_drafts_tenant_id ON shot_planning.shot_drafts USING btree (tenant_id);


--
-- Name: ix_shot_planning_shot_revisions_organization_id; Type: INDEX; Schema: shot_planning; Owner: -
--

CREATE INDEX ix_shot_planning_shot_revisions_organization_id ON shot_planning.shot_revisions USING btree (organization_id);


--
-- Name: ix_shot_planning_shot_revisions_project_id; Type: INDEX; Schema: shot_planning; Owner: -
--

CREATE INDEX ix_shot_planning_shot_revisions_project_id ON shot_planning.shot_revisions USING btree (project_id);


--
-- Name: ix_shot_planning_shot_revisions_shot_id; Type: INDEX; Schema: shot_planning; Owner: -
--

CREATE INDEX ix_shot_planning_shot_revisions_shot_id ON shot_planning.shot_revisions USING btree (shot_id);


--
-- Name: ix_shot_planning_shot_revisions_tenant_id; Type: INDEX; Schema: shot_planning; Owner: -
--

CREATE INDEX ix_shot_planning_shot_revisions_tenant_id ON shot_planning.shot_revisions USING btree (tenant_id);


--
-- Name: ix_shot_planning_shot_specs_organization_id; Type: INDEX; Schema: shot_planning; Owner: -
--

CREATE INDEX ix_shot_planning_shot_specs_organization_id ON shot_planning.shot_specs USING btree (organization_id);


--
-- Name: ix_shot_planning_shot_specs_project_id; Type: INDEX; Schema: shot_planning; Owner: -
--

CREATE INDEX ix_shot_planning_shot_specs_project_id ON shot_planning.shot_specs USING btree (project_id);


--
-- Name: ix_shot_planning_shot_specs_shot_id; Type: INDEX; Schema: shot_planning; Owner: -
--

CREATE INDEX ix_shot_planning_shot_specs_shot_id ON shot_planning.shot_specs USING btree (shot_id);


--
-- Name: ix_shot_planning_shot_specs_shot_revision_id; Type: INDEX; Schema: shot_planning; Owner: -
--

CREATE INDEX ix_shot_planning_shot_specs_shot_revision_id ON shot_planning.shot_specs USING btree (shot_revision_id);


--
-- Name: ix_shot_planning_shot_specs_tenant_id; Type: INDEX; Schema: shot_planning; Owner: -
--

CREATE INDEX ix_shot_planning_shot_specs_tenant_id ON shot_planning.shot_specs USING btree (tenant_id);


--
-- Name: ix_shot_planning_shot_visual_plans_organization_id; Type: INDEX; Schema: shot_planning; Owner: -
--

CREATE INDEX ix_shot_planning_shot_visual_plans_organization_id ON shot_planning.shot_visual_plans USING btree (organization_id);


--
-- Name: ix_shot_planning_shot_visual_plans_project_id; Type: INDEX; Schema: shot_planning; Owner: -
--

CREATE INDEX ix_shot_planning_shot_visual_plans_project_id ON shot_planning.shot_visual_plans USING btree (project_id);


--
-- Name: ix_shot_planning_shot_visual_plans_shot_id; Type: INDEX; Schema: shot_planning; Owner: -
--

CREATE INDEX ix_shot_planning_shot_visual_plans_shot_id ON shot_planning.shot_visual_plans USING btree (shot_id);


--
-- Name: ix_shot_planning_shot_visual_plans_tenant_id; Type: INDEX; Schema: shot_planning; Owner: -
--

CREATE INDEX ix_shot_planning_shot_visual_plans_tenant_id ON shot_planning.shot_visual_plans USING btree (tenant_id);


--
-- Name: uq_shot_planning_shot_revisions_shot_revision; Type: INDEX; Schema: shot_planning; Owner: -
--

CREATE UNIQUE INDEX uq_shot_planning_shot_revisions_shot_revision ON shot_planning.shot_revisions USING btree (shot_id, revision);


--
-- Name: ix_workflow_approval_flows_action_key; Type: INDEX; Schema: workflow; Owner: -
--

CREATE INDEX ix_workflow_approval_flows_action_key ON workflow.approval_flows USING btree (action_key);


--
-- Name: ix_workflow_approval_flows_organization_id; Type: INDEX; Schema: workflow; Owner: -
--

CREATE INDEX ix_workflow_approval_flows_organization_id ON workflow.approval_flows USING btree (organization_id);


--
-- Name: ix_workflow_approval_flows_resource; Type: INDEX; Schema: workflow; Owner: -
--

CREATE INDEX ix_workflow_approval_flows_resource ON workflow.approval_flows USING btree (resource_type, resource_id);


--
-- Name: ix_workflow_approval_flows_tenant_id; Type: INDEX; Schema: workflow; Owner: -
--

CREATE INDEX ix_workflow_approval_flows_tenant_id ON workflow.approval_flows USING btree (tenant_id);


--
-- Name: ix_workflow_approval_steps_organization_id; Type: INDEX; Schema: workflow; Owner: -
--

CREATE INDEX ix_workflow_approval_steps_organization_id ON workflow.approval_steps USING btree (organization_id);


--
-- Name: ix_workflow_approval_steps_tenant_id; Type: INDEX; Schema: workflow; Owner: -
--

CREATE INDEX ix_workflow_approval_steps_tenant_id ON workflow.approval_steps USING btree (tenant_id);


--
-- Name: ix_workflow_approval_steps_ticket_id; Type: INDEX; Schema: workflow; Owner: -
--

CREATE INDEX ix_workflow_approval_steps_ticket_id ON workflow.approval_steps USING btree (ticket_id);


--
-- Name: ix_workflow_approval_tickets_flow_id; Type: INDEX; Schema: workflow; Owner: -
--

CREATE INDEX ix_workflow_approval_tickets_flow_id ON workflow.approval_tickets USING btree (flow_id);


--
-- Name: ix_workflow_approval_tickets_organization_id; Type: INDEX; Schema: workflow; Owner: -
--

CREATE INDEX ix_workflow_approval_tickets_organization_id ON workflow.approval_tickets USING btree (organization_id);


--
-- Name: ix_workflow_approval_tickets_project; Type: INDEX; Schema: workflow; Owner: -
--

CREATE INDEX ix_workflow_approval_tickets_project ON workflow.approval_tickets USING btree (project_id);


--
-- Name: ix_workflow_approval_tickets_tenant_id; Type: INDEX; Schema: workflow; Owner: -
--

CREATE INDEX ix_workflow_approval_tickets_tenant_id ON workflow.approval_tickets USING btree (tenant_id);


--
-- Name: outbox_events_default_aggregate_id_idx; Type: INDEX ATTACH; Schema: outbox; Owner: -
--

ALTER INDEX outbox.ix_outbox_aggregate ATTACH PARTITION outbox.outbox_events_default_aggregate_id_idx;


--
-- Name: outbox_events_default_pkey; Type: INDEX ATTACH; Schema: outbox; Owner: -
--

ALTER INDEX outbox.outbox_events_pkey ATTACH PARTITION outbox.outbox_events_default_pkey;


--
-- Name: outbox_events_default_published_at_idx; Type: INDEX ATTACH; Schema: outbox; Owner: -
--

ALTER INDEX outbox.ix_outbox_published ATTACH PARTITION outbox.outbox_events_default_published_at_idx;


--
-- Name: outbox_events_default_tenant_id_idx; Type: INDEX ATTACH; Schema: outbox; Owner: -
--

ALTER INDEX outbox.ix_outbox_tenant ATTACH PARTITION outbox.outbox_events_default_tenant_id_idx;


--
-- Name: outbox_events_y202611_aggregate_id_idx; Type: INDEX ATTACH; Schema: outbox; Owner: -
--

ALTER INDEX outbox.ix_outbox_aggregate ATTACH PARTITION outbox.outbox_events_y202611_aggregate_id_idx;


--
-- Name: outbox_events_y202611_pkey; Type: INDEX ATTACH; Schema: outbox; Owner: -
--

ALTER INDEX outbox.outbox_events_pkey ATTACH PARTITION outbox.outbox_events_y202611_pkey;


--
-- Name: outbox_events_y202611_published_at_idx; Type: INDEX ATTACH; Schema: outbox; Owner: -
--

ALTER INDEX outbox.ix_outbox_published ATTACH PARTITION outbox.outbox_events_y202611_published_at_idx;


--
-- Name: outbox_events_y202611_tenant_id_idx; Type: INDEX ATTACH; Schema: outbox; Owner: -
--

ALTER INDEX outbox.ix_outbox_tenant ATTACH PARTITION outbox.outbox_events_y202611_tenant_id_idx;


--
-- Name: outbox_events_y202612_aggregate_id_idx; Type: INDEX ATTACH; Schema: outbox; Owner: -
--

ALTER INDEX outbox.ix_outbox_aggregate ATTACH PARTITION outbox.outbox_events_y202612_aggregate_id_idx;


--
-- Name: outbox_events_y202612_pkey; Type: INDEX ATTACH; Schema: outbox; Owner: -
--

ALTER INDEX outbox.outbox_events_pkey ATTACH PARTITION outbox.outbox_events_y202612_pkey;


--
-- Name: outbox_events_y202612_published_at_idx; Type: INDEX ATTACH; Schema: outbox; Owner: -
--

ALTER INDEX outbox.ix_outbox_published ATTACH PARTITION outbox.outbox_events_y202612_published_at_idx;


--
-- Name: outbox_events_y202612_tenant_id_idx; Type: INDEX ATTACH; Schema: outbox; Owner: -
--

ALTER INDEX outbox.ix_outbox_tenant ATTACH PARTITION outbox.outbox_events_y202612_tenant_id_idx;


--
-- Name: asset_versions asset_versions_project_assigned; Type: TRIGGER; Schema: asset_service; Owner: -
--

CREATE TRIGGER asset_versions_project_assigned BEFORE INSERT ON asset_service.asset_versions FOR EACH ROW EXECUTE FUNCTION asset_service.assert_asset_versions_project_assigned();


--
-- Name: qc_reports qc_reports_project_assigned; Type: TRIGGER; Schema: asset_service; Owner: -
--

CREATE TRIGGER qc_reports_project_assigned BEFORE INSERT ON asset_service.qc_reports FOR EACH ROW EXECUTE FUNCTION asset_service.assert_qc_reports_project_assigned();


--
-- Name: asset_versions trg_asset_versions_guard; Type: TRIGGER; Schema: asset_service; Owner: -
--

CREATE TRIGGER trg_asset_versions_guard BEFORE DELETE OR UPDATE ON asset_service.asset_versions FOR EACH ROW EXECUTE FUNCTION asset_service.asset_versions_guard();


--
-- Name: delivery_manifests trg_delivery_manifests_guard; Type: TRIGGER; Schema: asset_service; Owner: -
--

CREATE TRIGGER trg_delivery_manifests_guard BEFORE DELETE OR UPDATE ON asset_service.delivery_manifests FOR EACH ROW EXECUTE FUNCTION asset_service.delivery_manifests_guard();


--
-- Name: qc_reports trg_qc_reports_guard; Type: TRIGGER; Schema: asset_service; Owner: -
--

CREATE TRIGGER trg_qc_reports_guard BEFORE DELETE OR UPDATE ON asset_service.qc_reports FOR EACH ROW EXECUTE FUNCTION asset_service.qc_reports_guard();


--
-- Name: generation_tasks generation_tasks_guard_trigger; Type: TRIGGER; Schema: generation_execution; Owner: -
--

CREATE TRIGGER generation_tasks_guard_trigger BEFORE DELETE OR UPDATE ON generation_execution.generation_tasks FOR EACH ROW EXECUTE FUNCTION generation_execution.generation_tasks_guard();


--
-- Name: production_routes production_routes_guard_trigger; Type: TRIGGER; Schema: generation_execution; Owner: -
--

CREATE TRIGGER production_routes_guard_trigger BEFORE DELETE OR UPDATE ON generation_execution.production_routes FOR EACH ROW EXECUTE FUNCTION generation_execution.production_routes_guard();


--
-- Name: shot_route_selections shot_route_selections_guard_trigger; Type: TRIGGER; Schema: generation_execution; Owner: -
--

CREATE TRIGGER shot_route_selections_guard_trigger BEFORE DELETE OR UPDATE ON generation_execution.shot_route_selections FOR EACH ROW EXECUTE FUNCTION generation_execution.shot_route_selections_guard();


--
-- Name: shot_route_selections shot_route_selections_truncate_guard_trigger; Type: TRIGGER; Schema: generation_execution; Owner: -
--

CREATE TRIGGER shot_route_selections_truncate_guard_trigger BEFORE TRUNCATE ON generation_execution.shot_route_selections FOR EACH STATEMENT EXECUTE FUNCTION generation_execution.shot_route_selections_truncate_guard();


--
-- Name: qc_review_sessions qc_review_sessions_guard_trigger; Type: TRIGGER; Schema: qc_governance; Owner: -
--

CREATE TRIGGER qc_review_sessions_guard_trigger BEFORE DELETE OR UPDATE ON qc_governance.qc_review_sessions FOR EACH ROW EXECUTE FUNCTION qc_governance.qc_review_sessions_guard();


--
-- Name: qc_review_sessions qc_review_sessions_truncate_guard_trigger; Type: TRIGGER; Schema: qc_governance; Owner: -
--

CREATE TRIGGER qc_review_sessions_truncate_guard_trigger BEFORE TRUNCATE ON qc_governance.qc_review_sessions FOR EACH STATEMENT EXECUTE FUNCTION qc_governance.qc_review_sessions_truncate_guard();


--
-- Name: gate_decisions trg_gate_decisions_guard; Type: TRIGGER; Schema: qc_governance; Owner: -
--

CREATE TRIGGER trg_gate_decisions_guard BEFORE DELETE OR UPDATE ON qc_governance.gate_decisions FOR EACH ROW EXECUTE FUNCTION qc_governance.gate_decisions_guard();


--
-- Name: constraint_packs constraint_packs_guard_trigger; Type: TRIGGER; Schema: shot_planning; Owner: -
--

CREATE TRIGGER constraint_packs_guard_trigger BEFORE DELETE OR UPDATE ON shot_planning.constraint_packs FOR EACH ROW EXECUTE FUNCTION shot_planning.constraint_packs_guard();


--
-- Name: shot_specs shot_specs_guard_trigger; Type: TRIGGER; Schema: shot_planning; Owner: -
--

CREATE TRIGGER shot_specs_guard_trigger BEFORE DELETE OR UPDATE ON shot_planning.shot_specs FOR EACH ROW EXECUTE FUNCTION shot_planning.shot_specs_guard();


--
-- Name: shot_visual_plans shot_visual_plans_guard_trigger; Type: TRIGGER; Schema: shot_planning; Owner: -
--

CREATE TRIGGER shot_visual_plans_guard_trigger BEFORE DELETE OR UPDATE ON shot_planning.shot_visual_plans FOR EACH ROW EXECUTE FUNCTION shot_planning.shot_visual_plans_guard();


--
-- Name: approval_flows trg_approval_flows_guard; Type: TRIGGER; Schema: workflow; Owner: -
--

CREATE TRIGGER trg_approval_flows_guard BEFORE DELETE OR UPDATE ON workflow.approval_flows FOR EACH ROW EXECUTE FUNCTION workflow.approval_flows_guard();


--
-- Name: approval_steps trg_approval_steps_guard; Type: TRIGGER; Schema: workflow; Owner: -
--

CREATE TRIGGER trg_approval_steps_guard BEFORE DELETE OR UPDATE ON workflow.approval_steps FOR EACH ROW EXECUTE FUNCTION workflow.approval_steps_guard();


--
-- Name: qc_reports qc_reports_asset_version_id_fkey; Type: FK CONSTRAINT; Schema: asset_service; Owner: -
--

ALTER TABLE ONLY asset_service.qc_reports
    ADD CONSTRAINT qc_reports_asset_version_id_fkey FOREIGN KEY (asset_version_id) REFERENCES asset_service.asset_versions(entity_id);


--
-- Name: shot_route_selections fk_generation_execution_shot_route_selections_route_id; Type: FK CONSTRAINT; Schema: generation_execution; Owner: -
--

ALTER TABLE ONLY generation_execution.shot_route_selections
    ADD CONSTRAINT fk_generation_execution_shot_route_selections_route_id FOREIGN KEY (route_id) REFERENCES generation_execution.production_routes(entity_id) ON DELETE RESTRICT;


--
-- Name: script_revisions fk_script_revisions_episode; Type: FK CONSTRAINT; Schema: project_planning; Owner: -
--

ALTER TABLE ONLY project_planning.script_revisions
    ADD CONSTRAINT fk_script_revisions_episode FOREIGN KEY (project_id, episode_ref) REFERENCES project_planning.episodes(project_id, episode_ref);


--
-- Name: asset_versions; Type: ROW SECURITY; Schema: asset_service; Owner: -
--

ALTER TABLE asset_service.asset_versions ENABLE ROW LEVEL SECURITY;

--
-- Name: delivery_manifests; Type: ROW SECURITY; Schema: asset_service; Owner: -
--

ALTER TABLE asset_service.delivery_manifests ENABLE ROW LEVEL SECURITY;

--
-- Name: asset_versions project_scope_asset_versions; Type: POLICY; Schema: asset_service; Owner: -
--

CREATE POLICY project_scope_asset_versions ON asset_service.asset_versions AS RESTRICTIVE USING (((organization_id = (NULLIF(current_setting('app.current_org_id'::text, true), ''::text))::uuid) AND ((current_setting('app.current_projects'::text, true) = '*'::text) OR ((project_id)::text = ANY (string_to_array(NULLIF(current_setting('app.current_projects'::text, true), ''::text), ','::text)))))) WITH CHECK (((organization_id = (NULLIF(current_setting('app.current_org_id'::text, true), ''::text))::uuid) AND ((current_setting('app.current_projects'::text, true) = '*'::text) OR ((project_id)::text = ANY (string_to_array(NULLIF(current_setting('app.current_projects'::text, true), ''::text), ','::text))))));


--
-- Name: delivery_manifests project_scope_delivery_manifests; Type: POLICY; Schema: asset_service; Owner: -
--

CREATE POLICY project_scope_delivery_manifests ON asset_service.delivery_manifests AS RESTRICTIVE USING (((organization_id = (NULLIF(current_setting('app.current_org_id'::text, true), ''::text))::uuid) AND ((current_setting('app.current_projects'::text, true) = '*'::text) OR ((project_id)::text = ANY (string_to_array(NULLIF(current_setting('app.current_projects'::text, true), ''::text), ','::text)))))) WITH CHECK (((organization_id = (NULLIF(current_setting('app.current_org_id'::text, true), ''::text))::uuid) AND ((current_setting('app.current_projects'::text, true) = '*'::text) OR ((project_id)::text = ANY (string_to_array(NULLIF(current_setting('app.current_projects'::text, true), ''::text), ','::text))))));


--
-- Name: qc_reports project_scope_qc_reports; Type: POLICY; Schema: asset_service; Owner: -
--

CREATE POLICY project_scope_qc_reports ON asset_service.qc_reports AS RESTRICTIVE USING (((organization_id = (NULLIF(current_setting('app.current_org_id'::text, true), ''::text))::uuid) AND ((current_setting('app.current_projects'::text, true) = '*'::text) OR ((project_id)::text = ANY (string_to_array(NULLIF(current_setting('app.current_projects'::text, true), ''::text), ','::text)))))) WITH CHECK (((organization_id = (NULLIF(current_setting('app.current_org_id'::text, true), ''::text))::uuid) AND ((current_setting('app.current_projects'::text, true) = '*'::text) OR ((project_id)::text = ANY (string_to_array(NULLIF(current_setting('app.current_projects'::text, true), ''::text), ','::text))))));


--
-- Name: qc_reports; Type: ROW SECURITY; Schema: asset_service; Owner: -
--

ALTER TABLE asset_service.qc_reports ENABLE ROW LEVEL SECURITY;

--
-- Name: asset_versions tenant_isolation_asset_versions; Type: POLICY; Schema: asset_service; Owner: -
--

CREATE POLICY tenant_isolation_asset_versions ON asset_service.asset_versions USING ((tenant_id = (NULLIF(current_setting('app.current_tenant_id'::text, true), ''::text))::uuid));


--
-- Name: delivery_manifests tenant_isolation_delivery_manifests; Type: POLICY; Schema: asset_service; Owner: -
--

CREATE POLICY tenant_isolation_delivery_manifests ON asset_service.delivery_manifests USING ((tenant_id = (NULLIF(current_setting('app.current_tenant_id'::text, true), ''::text))::uuid));


--
-- Name: qc_reports tenant_isolation_qc_reports; Type: POLICY; Schema: asset_service; Owner: -
--

CREATE POLICY tenant_isolation_qc_reports ON asset_service.qc_reports USING ((tenant_id = (NULLIF(current_setting('app.current_tenant_id'::text, true), ''::text))::uuid));


--
-- Name: generation_tasks; Type: ROW SECURITY; Schema: generation_execution; Owner: -
--

ALTER TABLE generation_execution.generation_tasks ENABLE ROW LEVEL SECURITY;

--
-- Name: production_routes organization_scope_production_routes; Type: POLICY; Schema: generation_execution; Owner: -
--

CREATE POLICY organization_scope_production_routes ON generation_execution.production_routes AS RESTRICTIVE USING ((organization_id = (NULLIF(current_setting('app.current_org_id'::text, true), ''::text))::uuid)) WITH CHECK ((organization_id = (NULLIF(current_setting('app.current_org_id'::text, true), ''::text))::uuid));


--
-- Name: production_routes; Type: ROW SECURITY; Schema: generation_execution; Owner: -
--

ALTER TABLE generation_execution.production_routes ENABLE ROW LEVEL SECURITY;

--
-- Name: generation_tasks project_scope_generation_tasks; Type: POLICY; Schema: generation_execution; Owner: -
--

CREATE POLICY project_scope_generation_tasks ON generation_execution.generation_tasks AS RESTRICTIVE USING (((organization_id = (NULLIF(current_setting('app.current_org_id'::text, true), ''::text))::uuid) AND ((current_setting('app.current_projects'::text, true) = '*'::text) OR ((project_id)::text = ANY (string_to_array(NULLIF(current_setting('app.current_projects'::text, true), ''::text), ','::text)))))) WITH CHECK (((organization_id = (NULLIF(current_setting('app.current_org_id'::text, true), ''::text))::uuid) AND ((current_setting('app.current_projects'::text, true) = '*'::text) OR ((project_id)::text = ANY (string_to_array(NULLIF(current_setting('app.current_projects'::text, true), ''::text), ','::text))))));


--
-- Name: shot_route_selections project_scope_shot_route_selections; Type: POLICY; Schema: generation_execution; Owner: -
--

CREATE POLICY project_scope_shot_route_selections ON generation_execution.shot_route_selections AS RESTRICTIVE USING (((organization_id = (NULLIF(current_setting('app.current_org_id'::text, true), ''::text))::uuid) AND ((current_setting('app.current_projects'::text, true) = '*'::text) OR ((project_id)::text = ANY (string_to_array(NULLIF(current_setting('app.current_projects'::text, true), ''::text), ','::text)))))) WITH CHECK (((organization_id = (NULLIF(current_setting('app.current_org_id'::text, true), ''::text))::uuid) AND ((current_setting('app.current_projects'::text, true) = '*'::text) OR ((project_id)::text = ANY (string_to_array(NULLIF(current_setting('app.current_projects'::text, true), ''::text), ','::text))))));


--
-- Name: shot_route_selections; Type: ROW SECURITY; Schema: generation_execution; Owner: -
--

ALTER TABLE generation_execution.shot_route_selections ENABLE ROW LEVEL SECURITY;

--
-- Name: generation_tasks tenant_isolation_generation_tasks; Type: POLICY; Schema: generation_execution; Owner: -
--

CREATE POLICY tenant_isolation_generation_tasks ON generation_execution.generation_tasks USING ((tenant_id = (NULLIF(current_setting('app.current_tenant_id'::text, true), ''::text))::uuid));


--
-- Name: production_routes tenant_isolation_production_routes; Type: POLICY; Schema: generation_execution; Owner: -
--

CREATE POLICY tenant_isolation_production_routes ON generation_execution.production_routes USING ((tenant_id = (NULLIF(current_setting('app.current_tenant_id'::text, true), ''::text))::uuid));


--
-- Name: shot_route_selections tenant_isolation_shot_route_selections; Type: POLICY; Schema: generation_execution; Owner: -
--

CREATE POLICY tenant_isolation_shot_route_selections ON generation_execution.shot_route_selections USING ((tenant_id = (NULLIF(current_setting('app.current_tenant_id'::text, true), ''::text))::uuid));


--
-- Name: outbox_events; Type: ROW SECURITY; Schema: outbox; Owner: -
--

ALTER TABLE outbox.outbox_events ENABLE ROW LEVEL SECURITY;

--
-- Name: outbox_events_default; Type: ROW SECURITY; Schema: outbox; Owner: -
--

ALTER TABLE outbox.outbox_events_default ENABLE ROW LEVEL SECURITY;

--
-- Name: outbox_events_y202611; Type: ROW SECURITY; Schema: outbox; Owner: -
--

ALTER TABLE outbox.outbox_events_y202611 ENABLE ROW LEVEL SECURITY;

--
-- Name: outbox_events_y202612; Type: ROW SECURITY; Schema: outbox; Owner: -
--

ALTER TABLE outbox.outbox_events_y202612 ENABLE ROW LEVEL SECURITY;

--
-- Name: outbox_events tenant_isolation_outbox_events; Type: POLICY; Schema: outbox; Owner: -
--

CREATE POLICY tenant_isolation_outbox_events ON outbox.outbox_events USING ((tenant_id = (NULLIF(current_setting('app.current_tenant_id'::text, true), ''::text))::uuid));


--
-- Name: outbox_events_default tenant_isolation_outbox_events; Type: POLICY; Schema: outbox; Owner: -
--

CREATE POLICY tenant_isolation_outbox_events ON outbox.outbox_events_default USING ((tenant_id = (NULLIF(current_setting('app.current_tenant_id'::text, true), ''::text))::uuid));


--
-- Name: outbox_events_y202611 tenant_isolation_outbox_events; Type: POLICY; Schema: outbox; Owner: -
--

CREATE POLICY tenant_isolation_outbox_events ON outbox.outbox_events_y202611 USING ((tenant_id = (NULLIF(current_setting('app.current_tenant_id'::text, true), ''::text))::uuid));


--
-- Name: outbox_events_y202612 tenant_isolation_outbox_events; Type: POLICY; Schema: outbox; Owner: -
--

CREATE POLICY tenant_isolation_outbox_events ON outbox.outbox_events_y202612 USING ((tenant_id = (NULLIF(current_setting('app.current_tenant_id'::text, true), ''::text))::uuid));


--
-- Name: episodes; Type: ROW SECURITY; Schema: project_planning; Owner: -
--

ALTER TABLE project_planning.episodes ENABLE ROW LEVEL SECURITY;

--
-- Name: episodes project_scope_episodes; Type: POLICY; Schema: project_planning; Owner: -
--

CREATE POLICY project_scope_episodes ON project_planning.episodes AS RESTRICTIVE USING (((organization_id = (NULLIF(current_setting('app.current_org_id'::text, true), ''::text))::uuid) AND ((current_setting('app.current_projects'::text, true) = '*'::text) OR ((project_id)::text = ANY (string_to_array(NULLIF(current_setting('app.current_projects'::text, true), ''::text), ','::text)))))) WITH CHECK (((organization_id = (NULLIF(current_setting('app.current_org_id'::text, true), ''::text))::uuid) AND ((current_setting('app.current_projects'::text, true) = '*'::text) OR ((project_id)::text = ANY (string_to_array(NULLIF(current_setting('app.current_projects'::text, true), ''::text), ','::text))))));


--
-- Name: projects project_scope_projects; Type: POLICY; Schema: project_planning; Owner: -
--

CREATE POLICY project_scope_projects ON project_planning.projects AS RESTRICTIVE USING (((organization_id = (NULLIF(current_setting('app.current_org_id'::text, true), ''::text))::uuid) AND ((current_setting('app.current_projects'::text, true) = '*'::text) OR ((entity_id)::text = ANY (string_to_array(NULLIF(current_setting('app.current_projects'::text, true), ''::text), ','::text)))))) WITH CHECK (((organization_id = (NULLIF(current_setting('app.current_org_id'::text, true), ''::text))::uuid) AND ((current_setting('app.current_projects'::text, true) = '*'::text) OR ((entity_id)::text = ANY (string_to_array(NULLIF(current_setting('app.current_projects'::text, true), ''::text), ','::text))))));


--
-- Name: script_revisions project_scope_script_revisions; Type: POLICY; Schema: project_planning; Owner: -
--

CREATE POLICY project_scope_script_revisions ON project_planning.script_revisions AS RESTRICTIVE USING (((organization_id = (NULLIF(current_setting('app.current_org_id'::text, true), ''::text))::uuid) AND ((current_setting('app.current_projects'::text, true) = '*'::text) OR ((project_id)::text = ANY (string_to_array(NULLIF(current_setting('app.current_projects'::text, true), ''::text), ','::text)))))) WITH CHECK (((organization_id = (NULLIF(current_setting('app.current_org_id'::text, true), ''::text))::uuid) AND ((current_setting('app.current_projects'::text, true) = '*'::text) OR ((project_id)::text = ANY (string_to_array(NULLIF(current_setting('app.current_projects'::text, true), ''::text), ','::text))))));


--
-- Name: projects; Type: ROW SECURITY; Schema: project_planning; Owner: -
--

ALTER TABLE project_planning.projects ENABLE ROW LEVEL SECURITY;

--
-- Name: script_revisions; Type: ROW SECURITY; Schema: project_planning; Owner: -
--

ALTER TABLE project_planning.script_revisions ENABLE ROW LEVEL SECURITY;

--
-- Name: episodes tenant_isolation_episodes; Type: POLICY; Schema: project_planning; Owner: -
--

CREATE POLICY tenant_isolation_episodes ON project_planning.episodes USING ((tenant_id = (NULLIF(current_setting('app.current_tenant_id'::text, true), ''::text))::uuid));


--
-- Name: projects tenant_isolation_projects; Type: POLICY; Schema: project_planning; Owner: -
--

CREATE POLICY tenant_isolation_projects ON project_planning.projects USING ((tenant_id = (NULLIF(current_setting('app.current_tenant_id'::text, true), ''::text))::uuid));


--
-- Name: script_revisions tenant_isolation_script_revisions; Type: POLICY; Schema: project_planning; Owner: -
--

CREATE POLICY tenant_isolation_script_revisions ON project_planning.script_revisions USING ((tenant_id = (NULLIF(current_setting('app.current_tenant_id'::text, true), ''::text))::uuid));


--
-- Name: gate_decisions; Type: ROW SECURITY; Schema: qc_governance; Owner: -
--

ALTER TABLE qc_governance.gate_decisions ENABLE ROW LEVEL SECURITY;

--
-- Name: gate_decisions project_scope_gate_decisions; Type: POLICY; Schema: qc_governance; Owner: -
--

CREATE POLICY project_scope_gate_decisions ON qc_governance.gate_decisions AS RESTRICTIVE USING (((organization_id = (NULLIF(current_setting('app.current_org_id'::text, true), ''::text))::uuid) AND ((current_setting('app.current_projects'::text, true) = '*'::text) OR ((project_id)::text = ANY (string_to_array(NULLIF(current_setting('app.current_projects'::text, true), ''::text), ','::text)))))) WITH CHECK (((organization_id = (NULLIF(current_setting('app.current_org_id'::text, true), ''::text))::uuid) AND ((current_setting('app.current_projects'::text, true) = '*'::text) OR ((project_id)::text = ANY (string_to_array(NULLIF(current_setting('app.current_projects'::text, true), ''::text), ','::text))))));


--
-- Name: qc_review_sessions project_scope_qc_review_sessions; Type: POLICY; Schema: qc_governance; Owner: -
--

CREATE POLICY project_scope_qc_review_sessions ON qc_governance.qc_review_sessions AS RESTRICTIVE USING (((organization_id = (NULLIF(current_setting('app.current_org_id'::text, true), ''::text))::uuid) AND ((current_setting('app.current_projects'::text, true) = '*'::text) OR ((project_id)::text = ANY (string_to_array(NULLIF(current_setting('app.current_projects'::text, true), ''::text), ','::text)))))) WITH CHECK (((organization_id = (NULLIF(current_setting('app.current_org_id'::text, true), ''::text))::uuid) AND ((current_setting('app.current_projects'::text, true) = '*'::text) OR ((project_id)::text = ANY (string_to_array(NULLIF(current_setting('app.current_projects'::text, true), ''::text), ','::text))))));


--
-- Name: qc_review_sessions; Type: ROW SECURITY; Schema: qc_governance; Owner: -
--

ALTER TABLE qc_governance.qc_review_sessions ENABLE ROW LEVEL SECURITY;

--
-- Name: gate_decisions tenant_isolation_gate_decisions; Type: POLICY; Schema: qc_governance; Owner: -
--

CREATE POLICY tenant_isolation_gate_decisions ON qc_governance.gate_decisions USING ((tenant_id = (NULLIF(current_setting('app.current_tenant_id'::text, true), ''::text))::uuid));


--
-- Name: qc_review_sessions tenant_isolation_qc_review_sessions; Type: POLICY; Schema: qc_governance; Owner: -
--

CREATE POLICY tenant_isolation_qc_review_sessions ON qc_governance.qc_review_sessions USING ((tenant_id = (NULLIF(current_setting('app.current_tenant_id'::text, true), ''::text))::uuid));


--
-- Name: constraint_packs; Type: ROW SECURITY; Schema: shot_planning; Owner: -
--

ALTER TABLE shot_planning.constraint_packs ENABLE ROW LEVEL SECURITY;

--
-- Name: constraint_packs project_scope_constraint_packs; Type: POLICY; Schema: shot_planning; Owner: -
--

CREATE POLICY project_scope_constraint_packs ON shot_planning.constraint_packs AS RESTRICTIVE USING (((organization_id = (NULLIF(current_setting('app.current_org_id'::text, true), ''::text))::uuid) AND ((current_setting('app.current_projects'::text, true) = '*'::text) OR ((project_id)::text = ANY (string_to_array(NULLIF(current_setting('app.current_projects'::text, true), ''::text), ','::text)))))) WITH CHECK (((organization_id = (NULLIF(current_setting('app.current_org_id'::text, true), ''::text))::uuid) AND ((current_setting('app.current_projects'::text, true) = '*'::text) OR ((project_id)::text = ANY (string_to_array(NULLIF(current_setting('app.current_projects'::text, true), ''::text), ','::text))))));


--
-- Name: shot_drafts project_scope_shot_drafts; Type: POLICY; Schema: shot_planning; Owner: -
--

CREATE POLICY project_scope_shot_drafts ON shot_planning.shot_drafts AS RESTRICTIVE USING (((organization_id = (NULLIF(current_setting('app.current_org_id'::text, true), ''::text))::uuid) AND ((current_setting('app.current_projects'::text, true) = '*'::text) OR ((project_id)::text = ANY (string_to_array(NULLIF(current_setting('app.current_projects'::text, true), ''::text), ','::text)))))) WITH CHECK (((organization_id = (NULLIF(current_setting('app.current_org_id'::text, true), ''::text))::uuid) AND ((current_setting('app.current_projects'::text, true) = '*'::text) OR ((project_id)::text = ANY (string_to_array(NULLIF(current_setting('app.current_projects'::text, true), ''::text), ','::text))))));


--
-- Name: shot_revisions project_scope_shot_revisions; Type: POLICY; Schema: shot_planning; Owner: -
--

CREATE POLICY project_scope_shot_revisions ON shot_planning.shot_revisions AS RESTRICTIVE USING (((organization_id = (NULLIF(current_setting('app.current_org_id'::text, true), ''::text))::uuid) AND ((current_setting('app.current_projects'::text, true) = '*'::text) OR ((project_id)::text = ANY (string_to_array(NULLIF(current_setting('app.current_projects'::text, true), ''::text), ','::text)))))) WITH CHECK (((organization_id = (NULLIF(current_setting('app.current_org_id'::text, true), ''::text))::uuid) AND ((current_setting('app.current_projects'::text, true) = '*'::text) OR ((project_id)::text = ANY (string_to_array(NULLIF(current_setting('app.current_projects'::text, true), ''::text), ','::text))))));


--
-- Name: shot_specs project_scope_shot_specs; Type: POLICY; Schema: shot_planning; Owner: -
--

CREATE POLICY project_scope_shot_specs ON shot_planning.shot_specs AS RESTRICTIVE USING (((organization_id = (NULLIF(current_setting('app.current_org_id'::text, true), ''::text))::uuid) AND ((current_setting('app.current_projects'::text, true) = '*'::text) OR ((project_id)::text = ANY (string_to_array(NULLIF(current_setting('app.current_projects'::text, true), ''::text), ','::text)))))) WITH CHECK (((organization_id = (NULLIF(current_setting('app.current_org_id'::text, true), ''::text))::uuid) AND ((current_setting('app.current_projects'::text, true) = '*'::text) OR ((project_id)::text = ANY (string_to_array(NULLIF(current_setting('app.current_projects'::text, true), ''::text), ','::text))))));


--
-- Name: shot_visual_plans project_scope_shot_visual_plans; Type: POLICY; Schema: shot_planning; Owner: -
--

CREATE POLICY project_scope_shot_visual_plans ON shot_planning.shot_visual_plans AS RESTRICTIVE USING (((organization_id = (NULLIF(current_setting('app.current_org_id'::text, true), ''::text))::uuid) AND ((current_setting('app.current_projects'::text, true) = '*'::text) OR ((project_id)::text = ANY (string_to_array(NULLIF(current_setting('app.current_projects'::text, true), ''::text), ','::text)))))) WITH CHECK (((organization_id = (NULLIF(current_setting('app.current_org_id'::text, true), ''::text))::uuid) AND ((current_setting('app.current_projects'::text, true) = '*'::text) OR ((project_id)::text = ANY (string_to_array(NULLIF(current_setting('app.current_projects'::text, true), ''::text), ','::text))))));


--
-- Name: shot_drafts; Type: ROW SECURITY; Schema: shot_planning; Owner: -
--

ALTER TABLE shot_planning.shot_drafts ENABLE ROW LEVEL SECURITY;

--
-- Name: shot_revisions; Type: ROW SECURITY; Schema: shot_planning; Owner: -
--

ALTER TABLE shot_planning.shot_revisions ENABLE ROW LEVEL SECURITY;

--
-- Name: shot_specs; Type: ROW SECURITY; Schema: shot_planning; Owner: -
--

ALTER TABLE shot_planning.shot_specs ENABLE ROW LEVEL SECURITY;

--
-- Name: shot_visual_plans; Type: ROW SECURITY; Schema: shot_planning; Owner: -
--

ALTER TABLE shot_planning.shot_visual_plans ENABLE ROW LEVEL SECURITY;

--
-- Name: constraint_packs tenant_isolation_constraint_packs; Type: POLICY; Schema: shot_planning; Owner: -
--

CREATE POLICY tenant_isolation_constraint_packs ON shot_planning.constraint_packs USING ((tenant_id = (NULLIF(current_setting('app.current_tenant_id'::text, true), ''::text))::uuid));


--
-- Name: shot_drafts tenant_isolation_shot_drafts; Type: POLICY; Schema: shot_planning; Owner: -
--

CREATE POLICY tenant_isolation_shot_drafts ON shot_planning.shot_drafts USING ((tenant_id = (NULLIF(current_setting('app.current_tenant_id'::text, true), ''::text))::uuid));


--
-- Name: shot_revisions tenant_isolation_shot_revisions; Type: POLICY; Schema: shot_planning; Owner: -
--

CREATE POLICY tenant_isolation_shot_revisions ON shot_planning.shot_revisions USING ((tenant_id = (NULLIF(current_setting('app.current_tenant_id'::text, true), ''::text))::uuid));


--
-- Name: shot_specs tenant_isolation_shot_specs; Type: POLICY; Schema: shot_planning; Owner: -
--

CREATE POLICY tenant_isolation_shot_specs ON shot_planning.shot_specs USING ((tenant_id = (NULLIF(current_setting('app.current_tenant_id'::text, true), ''::text))::uuid));


--
-- Name: shot_visual_plans tenant_isolation_shot_visual_plans; Type: POLICY; Schema: shot_planning; Owner: -
--

CREATE POLICY tenant_isolation_shot_visual_plans ON shot_planning.shot_visual_plans USING ((tenant_id = (NULLIF(current_setting('app.current_tenant_id'::text, true), ''::text))::uuid));


--
-- Name: approval_flows; Type: ROW SECURITY; Schema: workflow; Owner: -
--

ALTER TABLE workflow.approval_flows ENABLE ROW LEVEL SECURITY;

--
-- Name: approval_steps; Type: ROW SECURITY; Schema: workflow; Owner: -
--

ALTER TABLE workflow.approval_steps ENABLE ROW LEVEL SECURITY;

--
-- Name: approval_tickets; Type: ROW SECURITY; Schema: workflow; Owner: -
--

ALTER TABLE workflow.approval_tickets ENABLE ROW LEVEL SECURITY;

--
-- Name: approval_flows project_scope_approval_flows; Type: POLICY; Schema: workflow; Owner: -
--

CREATE POLICY project_scope_approval_flows ON workflow.approval_flows AS RESTRICTIVE USING (((organization_id = (NULLIF(current_setting('app.current_org_id'::text, true), ''::text))::uuid) AND ((current_setting('app.current_projects'::text, true) = '*'::text) OR ((project_id)::text = ANY (string_to_array(NULLIF(current_setting('app.current_projects'::text, true), ''::text), ','::text)))))) WITH CHECK (((organization_id = (NULLIF(current_setting('app.current_org_id'::text, true), ''::text))::uuid) AND ((current_setting('app.current_projects'::text, true) = '*'::text) OR ((project_id)::text = ANY (string_to_array(NULLIF(current_setting('app.current_projects'::text, true), ''::text), ','::text))))));


--
-- Name: approval_steps project_scope_approval_steps; Type: POLICY; Schema: workflow; Owner: -
--

CREATE POLICY project_scope_approval_steps ON workflow.approval_steps AS RESTRICTIVE USING (((organization_id = (NULLIF(current_setting('app.current_org_id'::text, true), ''::text))::uuid) AND ((current_setting('app.current_projects'::text, true) = '*'::text) OR ((project_id)::text = ANY (string_to_array(NULLIF(current_setting('app.current_projects'::text, true), ''::text), ','::text)))))) WITH CHECK (((organization_id = (NULLIF(current_setting('app.current_org_id'::text, true), ''::text))::uuid) AND ((current_setting('app.current_projects'::text, true) = '*'::text) OR ((project_id)::text = ANY (string_to_array(NULLIF(current_setting('app.current_projects'::text, true), ''::text), ','::text))))));


--
-- Name: approval_tickets project_scope_approval_tickets; Type: POLICY; Schema: workflow; Owner: -
--

CREATE POLICY project_scope_approval_tickets ON workflow.approval_tickets AS RESTRICTIVE USING (((organization_id = (NULLIF(current_setting('app.current_org_id'::text, true), ''::text))::uuid) AND ((current_setting('app.current_projects'::text, true) = '*'::text) OR ((project_id)::text = ANY (string_to_array(NULLIF(current_setting('app.current_projects'::text, true), ''::text), ','::text)))))) WITH CHECK (((organization_id = (NULLIF(current_setting('app.current_org_id'::text, true), ''::text))::uuid) AND ((current_setting('app.current_projects'::text, true) = '*'::text) OR ((project_id)::text = ANY (string_to_array(NULLIF(current_setting('app.current_projects'::text, true), ''::text), ','::text))))));


--
-- Name: approval_flows tenant_isolation_approval_flows; Type: POLICY; Schema: workflow; Owner: -
--

CREATE POLICY tenant_isolation_approval_flows ON workflow.approval_flows USING ((tenant_id = (NULLIF(current_setting('app.current_tenant_id'::text, true), ''::text))::uuid));


--
-- Name: approval_steps tenant_isolation_approval_steps; Type: POLICY; Schema: workflow; Owner: -
--

CREATE POLICY tenant_isolation_approval_steps ON workflow.approval_steps USING ((tenant_id = (NULLIF(current_setting('app.current_tenant_id'::text, true), ''::text))::uuid));


--
-- Name: approval_tickets tenant_isolation_approval_tickets; Type: POLICY; Schema: workflow; Owner: -
--

CREATE POLICY tenant_isolation_approval_tickets ON workflow.approval_tickets USING ((tenant_id = (NULLIF(current_setting('app.current_tenant_id'::text, true), ''::text))::uuid));


--
-- PostgreSQL database dump complete
--

\unrestrict q20osEojBg36YyJ6NPl2VYwnKGtGThA5tgpiou8yCCENhEhOaNV9E6azNYJymbm

