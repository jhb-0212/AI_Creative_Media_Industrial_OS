--
-- PostgreSQL database dump
--

\restrict bzmYe0yilFVZZ2FUjR8ubOq1dEcFRwWzlD49NhVm51JldHx1eEKOI89VtQFZyZT

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
-- Name: audit; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA audit;


--
-- Name: auth; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA auth;


--
-- Name: outbox; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA outbox;


--
-- Name: project_planning; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA project_planning;


--
-- Name: asset_versions_guard(); Type: FUNCTION; Schema: asset_service; Owner: -
--

CREATE FUNCTION asset_service.asset_versions_guard() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
DECLARE
    col text;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION
            'asset_versions 是追加型不可变表，禁止删除（ADR-R11）';
    END IF;

    -- 逐列扫描当前表的真实列，新增内容列自动纳入保护，不依赖手写清单。
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
-- Name: assert_transition_event_exists(); Type: FUNCTION; Schema: audit; Owner: -
--

CREATE FUNCTION audit.assert_transition_event_exists() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'outbox'
    AS $$
        DECLARE
            found_event uuid;
        BEGIN
            SELECT event_id INTO found_event
            FROM outbox.outbox_events
            WHERE event_id = NEW.event_id
              AND tenant_id = NEW.tenant_id;

            IF found_event IS NULL THEN
                RAISE EXCEPTION
                    '状态迁移事实 % 指向的事件 % 不存在于租户 %：迁移与事件必须同事务写入同一租户',
                    NEW.transition_id, NEW.event_id, NEW.tenant_id
                    USING ERRCODE = 'foreign_key_violation';
            END IF;
            RETURN NULL;
        END;
        $$;


--
-- Name: reject_mutation(); Type: FUNCTION; Schema: audit; Owner: -
--

CREATE FUNCTION audit.reject_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            RAISE EXCEPTION
                'append-only 表禁止 % (表 %)', TG_OP, TG_TABLE_SCHEMA || '.' || TG_TABLE_NAME
                USING ERRCODE = 'restrict_violation';
        END;
        $$;


--
-- Name: reject_truncate(); Type: FUNCTION; Schema: audit; Owner: -
--

CREATE FUNCTION audit.reject_truncate() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            RAISE EXCEPTION
                'append-only 表禁止 TRUNCATE (表 %)', TG_TABLE_SCHEMA || '.' || TG_TABLE_NAME
                USING ERRCODE = 'restrict_violation';
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


SET default_tablespace = '';

SET default_table_access_method = heap;

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
    project_id character varying(64) NOT NULL,
    CONSTRAINT ck_asset_versions_status CHECK (((status)::text = ANY ((ARRAY['APPROVED'::character varying, 'ARCHIVED'::character varying, 'GENERATED'::character varying, 'GOLD'::character varying, 'HUMAN_REVIEW'::character varying, 'QUARANTINED'::character varying, 'REJECTED'::character varying, 'REWORK'::character varying, 'SUPERSEDED'::character varying, 'TECHNICAL_QC'::character varying])::text[])))
);

ALTER TABLE ONLY asset_service.asset_versions FORCE ROW LEVEL SECURITY;


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
    deviation_intent_ref character varying(64)
);

ALTER TABLE ONLY asset_service.qc_reports FORCE ROW LEVEL SECURITY;


--
-- Name: status_transitions; Type: TABLE; Schema: audit; Owner: -
--

CREATE TABLE audit.status_transitions (
    transition_id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    organization_id uuid,
    resource_type character varying(64) NOT NULL,
    resource_id character varying(64) NOT NULL,
    entity_version integer NOT NULL,
    from_status character varying(64) NOT NULL,
    to_status character varying(64) NOT NULL,
    owner_action character varying(64) NOT NULL,
    actor_subject character varying(128) NOT NULL,
    authority_ref character varying(128),
    evidence_refs character varying(64)[] DEFAULT '{}'::character varying[] NOT NULL,
    occurred_at timestamp with time zone DEFAULT now() NOT NULL,
    event_id uuid NOT NULL,
    CONSTRAINT ck_status_transition_advances CHECK (((from_status)::text <> (to_status)::text)),
    CONSTRAINT ck_status_transition_version_positive CHECK ((entity_version >= 1))
);

ALTER TABLE ONLY audit.status_transitions FORCE ROW LEVEL SECURITY;


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
    CONSTRAINT ck_episodes_status CHECK (((status)::text = ANY ((ARRAY['ACTIVE'::character varying, 'ARCHIVED'::character varying, 'DRAFT'::character varying])::text[])))
);

ALTER TABLE ONLY project_planning.episodes FORCE ROW LEVEL SECURITY;


--
-- Name: organizations; Type: TABLE; Schema: project_planning; Owner: -
--

CREATE TABLE project_planning.organizations (
    organization_id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    name character varying(200) NOT NULL,
    created_at timestamp with time zone NOT NULL,
    created_by character varying(64) NOT NULL,
    updated_at timestamp with time zone NOT NULL
);

ALTER TABLE ONLY project_planning.organizations FORCE ROW LEVEL SECURITY;


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
    organization_id uuid,
    CONSTRAINT ck_projects_status CHECK (((status)::text = ANY ((ARRAY['ACTIVE'::character varying, 'ARCHIVED'::character varying, 'COMPLETED'::character varying, 'DRAFT'::character varying, 'ON_HOLD'::character varying])::text[])))
);

ALTER TABLE ONLY project_planning.projects FORCE ROW LEVEL SECURITY;


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
    CONSTRAINT ck_script_revisions_status CHECK (((status)::text = ANY ((ARRAY['DRAFT'::character varying, 'LOCKED'::character varying, 'LOCK_PENDING'::character varying, 'SUPERSEDED'::character varying])::text[])))
);

ALTER TABLE ONLY project_planning.script_revisions FORCE ROW LEVEL SECURITY;


--
-- Name: alembic_version; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.alembic_version (
    version_num character varying(32) NOT NULL
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
-- Name: asset_versions asset_versions_pkey; Type: CONSTRAINT; Schema: asset_service; Owner: -
--

ALTER TABLE ONLY asset_service.asset_versions
    ADD CONSTRAINT asset_versions_pkey PRIMARY KEY (entity_id);


--
-- Name: qc_reports qc_reports_pkey; Type: CONSTRAINT; Schema: asset_service; Owner: -
--

ALTER TABLE ONLY asset_service.qc_reports
    ADD CONSTRAINT qc_reports_pkey PRIMARY KEY (qc_report_id);


--
-- Name: status_transitions status_transitions_pkey; Type: CONSTRAINT; Schema: audit; Owner: -
--

ALTER TABLE ONLY audit.status_transitions
    ADD CONSTRAINT status_transitions_pkey PRIMARY KEY (transition_id);


--
-- Name: status_transitions uq_status_transition_version; Type: CONSTRAINT; Schema: audit; Owner: -
--

ALTER TABLE ONLY audit.status_transitions
    ADD CONSTRAINT uq_status_transition_version UNIQUE (tenant_id, resource_type, resource_id, entity_version);


--
-- Name: revoked_tokens revoked_tokens_pkey; Type: CONSTRAINT; Schema: auth; Owner: -
--

ALTER TABLE ONLY auth.revoked_tokens
    ADD CONSTRAINT revoked_tokens_pkey PRIMARY KEY (jti);


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
-- Name: organizations organizations_pkey; Type: CONSTRAINT; Schema: project_planning; Owner: -
--

ALTER TABLE ONLY project_planning.organizations
    ADD CONSTRAINT organizations_pkey PRIMARY KEY (organization_id);


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
-- Name: organizations uq_organizations_owner_tenant; Type: CONSTRAINT; Schema: project_planning; Owner: -
--

ALTER TABLE ONLY project_planning.organizations
    ADD CONSTRAINT uq_organizations_owner_tenant UNIQUE (organization_id, tenant_id);


--
-- Name: projects uq_projects_entity_tenant; Type: CONSTRAINT; Schema: project_planning; Owner: -
--

ALTER TABLE ONLY project_planning.projects
    ADD CONSTRAINT uq_projects_entity_tenant UNIQUE (entity_id, tenant_id);


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
-- Name: ix_asset_service_asset_versions_project; Type: INDEX; Schema: asset_service; Owner: -
--

CREATE INDEX ix_asset_service_asset_versions_project ON asset_service.asset_versions USING btree (project_id);


--
-- Name: ix_asset_service_asset_versions_tenant_id; Type: INDEX; Schema: asset_service; Owner: -
--

CREATE INDEX ix_asset_service_asset_versions_tenant_id ON asset_service.asset_versions USING btree (tenant_id);


--
-- Name: ix_asset_service_asset_versions_type_sha256; Type: INDEX; Schema: asset_service; Owner: -
--

CREATE INDEX ix_asset_service_asset_versions_type_sha256 ON asset_service.asset_versions USING btree (asset_type, sha256);


--
-- Name: ix_asset_service_qc_reports_asset_version_id; Type: INDEX; Schema: asset_service; Owner: -
--

CREATE INDEX ix_asset_service_qc_reports_asset_version_id ON asset_service.qc_reports USING btree (asset_version_id);


--
-- Name: ix_asset_service_qc_reports_tenant_id; Type: INDEX; Schema: asset_service; Owner: -
--

CREATE INDEX ix_asset_service_qc_reports_tenant_id ON asset_service.qc_reports USING btree (tenant_id);


--
-- Name: ix_status_transitions_resource_history; Type: INDEX; Schema: audit; Owner: -
--

CREATE INDEX ix_status_transitions_resource_history ON audit.status_transitions USING btree (tenant_id, resource_type, resource_id, occurred_at);


--
-- Name: ix_auth_revoked_tokens_expires_at; Type: INDEX; Schema: auth; Owner: -
--

CREATE INDEX ix_auth_revoked_tokens_expires_at ON auth.revoked_tokens USING btree (expires_at);


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
-- Name: ix_project_planning_episodes_project_id; Type: INDEX; Schema: project_planning; Owner: -
--

CREATE INDEX ix_project_planning_episodes_project_id ON project_planning.episodes USING btree (project_id);


--
-- Name: ix_project_planning_episodes_tenant_id; Type: INDEX; Schema: project_planning; Owner: -
--

CREATE INDEX ix_project_planning_episodes_tenant_id ON project_planning.episodes USING btree (tenant_id);


--
-- Name: ix_project_planning_organizations_tenant_id; Type: INDEX; Schema: project_planning; Owner: -
--

CREATE INDEX ix_project_planning_organizations_tenant_id ON project_planning.organizations USING btree (tenant_id);


--
-- Name: ix_project_planning_projects_tenant_id; Type: INDEX; Schema: project_planning; Owner: -
--

CREATE INDEX ix_project_planning_projects_tenant_id ON project_planning.projects USING btree (tenant_id);


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
-- Name: asset_versions trg_asset_versions_guard; Type: TRIGGER; Schema: asset_service; Owner: -
--

CREATE TRIGGER trg_asset_versions_guard BEFORE DELETE OR UPDATE ON asset_service.asset_versions FOR EACH ROW EXECUTE FUNCTION asset_service.asset_versions_guard();


--
-- Name: asset_versions trg_asset_versions_no_truncate; Type: TRIGGER; Schema: asset_service; Owner: -
--

CREATE TRIGGER trg_asset_versions_no_truncate BEFORE TRUNCATE ON asset_service.asset_versions FOR EACH STATEMENT EXECUTE FUNCTION audit.reject_truncate();


--
-- Name: qc_reports trg_qc_reports_guard; Type: TRIGGER; Schema: asset_service; Owner: -
--

CREATE TRIGGER trg_qc_reports_guard BEFORE DELETE OR UPDATE ON asset_service.qc_reports FOR EACH ROW EXECUTE FUNCTION asset_service.qc_reports_guard();


--
-- Name: qc_reports trg_qc_reports_no_truncate; Type: TRIGGER; Schema: asset_service; Owner: -
--

CREATE TRIGGER trg_qc_reports_no_truncate BEFORE TRUNCATE ON asset_service.qc_reports FOR EACH STATEMENT EXECUTE FUNCTION audit.reject_truncate();


--
-- Name: status_transitions trg_status_transitions_event_exists; Type: TRIGGER; Schema: audit; Owner: -
--

CREATE CONSTRAINT TRIGGER trg_status_transitions_event_exists AFTER INSERT ON audit.status_transitions DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION audit.assert_transition_event_exists();


--
-- Name: status_transitions trg_status_transitions_guard; Type: TRIGGER; Schema: audit; Owner: -
--

CREATE TRIGGER trg_status_transitions_guard BEFORE DELETE OR UPDATE ON audit.status_transitions FOR EACH ROW EXECUTE FUNCTION audit.reject_mutation();


--
-- Name: status_transitions trg_status_transitions_no_truncate; Type: TRIGGER; Schema: audit; Owner: -
--

CREATE TRIGGER trg_status_transitions_no_truncate BEFORE TRUNCATE ON audit.status_transitions FOR EACH STATEMENT EXECUTE FUNCTION audit.reject_truncate();


--
-- Name: asset_versions fk_asset_versions_project_tenant; Type: FK CONSTRAINT; Schema: asset_service; Owner: -
--

ALTER TABLE ONLY asset_service.asset_versions
    ADD CONSTRAINT fk_asset_versions_project_tenant FOREIGN KEY (project_id, tenant_id) REFERENCES project_planning.projects(entity_id, tenant_id);


--
-- Name: qc_reports qc_reports_asset_version_id_fkey; Type: FK CONSTRAINT; Schema: asset_service; Owner: -
--

ALTER TABLE ONLY asset_service.qc_reports
    ADD CONSTRAINT qc_reports_asset_version_id_fkey FOREIGN KEY (asset_version_id) REFERENCES asset_service.asset_versions(entity_id);


--
-- Name: episodes fk_episodes_project; Type: FK CONSTRAINT; Schema: project_planning; Owner: -
--

ALTER TABLE ONLY project_planning.episodes
    ADD CONSTRAINT fk_episodes_project FOREIGN KEY (project_id) REFERENCES project_planning.projects(entity_id);


--
-- Name: projects fk_projects_organization_tenant; Type: FK CONSTRAINT; Schema: project_planning; Owner: -
--

ALTER TABLE ONLY project_planning.projects
    ADD CONSTRAINT fk_projects_organization_tenant FOREIGN KEY (organization_id, tenant_id) REFERENCES project_planning.organizations(organization_id, tenant_id);


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
-- Name: qc_reports; Type: ROW SECURITY; Schema: asset_service; Owner: -
--

ALTER TABLE asset_service.qc_reports ENABLE ROW LEVEL SECURITY;

--
-- Name: asset_versions tenant_isolation_asset_versions; Type: POLICY; Schema: asset_service; Owner: -
--

CREATE POLICY tenant_isolation_asset_versions ON asset_service.asset_versions USING (((tenant_id = (NULLIF(current_setting('app.current_tenant_id'::text, true), ''::text))::uuid) AND (EXISTS ( SELECT 1
   FROM project_planning.projects p
  WHERE (((p.entity_id)::text = (asset_versions.project_id)::text) AND (p.tenant_id = asset_versions.tenant_id))))));


--
-- Name: qc_reports tenant_isolation_qc_reports; Type: POLICY; Schema: asset_service; Owner: -
--

CREATE POLICY tenant_isolation_qc_reports ON asset_service.qc_reports USING (((tenant_id = (NULLIF(current_setting('app.current_tenant_id'::text, true), ''::text))::uuid) AND (EXISTS ( SELECT 1
   FROM (asset_service.asset_versions a
     JOIN project_planning.projects p ON ((((p.entity_id)::text = (a.project_id)::text) AND (p.tenant_id = a.tenant_id))))
  WHERE (((a.entity_id)::text = (qc_reports.asset_version_id)::text) AND (a.tenant_id = qc_reports.tenant_id))))));


--
-- Name: status_transitions; Type: ROW SECURITY; Schema: audit; Owner: -
--

ALTER TABLE audit.status_transitions ENABLE ROW LEVEL SECURITY;

--
-- Name: status_transitions tenant_isolation_status_transitions; Type: POLICY; Schema: audit; Owner: -
--

CREATE POLICY tenant_isolation_status_transitions ON audit.status_transitions USING ((tenant_id = (NULLIF(current_setting('app.current_tenant_id'::text, true), ''::text))::uuid));


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
-- Name: organizations; Type: ROW SECURITY; Schema: project_planning; Owner: -
--

ALTER TABLE project_planning.organizations ENABLE ROW LEVEL SECURITY;

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

CREATE POLICY tenant_isolation_episodes ON project_planning.episodes USING (((tenant_id = (NULLIF(current_setting('app.current_tenant_id'::text, true), ''::text))::uuid) AND (EXISTS ( SELECT 1
   FROM project_planning.projects p
  WHERE (((p.entity_id)::text = (episodes.project_id)::text) AND (p.tenant_id = episodes.tenant_id))))));


--
-- Name: organizations tenant_isolation_organizations; Type: POLICY; Schema: project_planning; Owner: -
--

CREATE POLICY tenant_isolation_organizations ON project_planning.organizations USING (((tenant_id = (NULLIF(current_setting('app.current_tenant_id'::text, true), ''::text))::uuid) AND ((organization_id IS NULL) OR (organization_id = (NULLIF(current_setting('app.current_organization_id'::text, true), ''::text))::uuid))));


--
-- Name: projects tenant_isolation_projects; Type: POLICY; Schema: project_planning; Owner: -
--

CREATE POLICY tenant_isolation_projects ON project_planning.projects USING (((tenant_id = (NULLIF(current_setting('app.current_tenant_id'::text, true), ''::text))::uuid) AND ((organization_id IS NULL) OR (organization_id = (NULLIF(current_setting('app.current_organization_id'::text, true), ''::text))::uuid))));


--
-- Name: script_revisions tenant_isolation_script_revisions; Type: POLICY; Schema: project_planning; Owner: -
--

CREATE POLICY tenant_isolation_script_revisions ON project_planning.script_revisions USING (((tenant_id = (NULLIF(current_setting('app.current_tenant_id'::text, true), ''::text))::uuid) AND (EXISTS ( SELECT 1
   FROM project_planning.projects p
  WHERE (((p.entity_id)::text = (script_revisions.project_id)::text) AND (p.tenant_id = script_revisions.tenant_id))))));


--
-- PostgreSQL database dump complete
--

\unrestrict bzmYe0yilFVZZ2FUjR8ubOq1dEcFRwWzlD49NhVm51JldHx1eEKOI89VtQFZyZT

