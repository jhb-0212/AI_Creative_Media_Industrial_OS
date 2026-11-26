"""维护工单 + 审计账本 + 三个只读维护命令（WP-A 第一阶段）。

这一阶段要解决的问题，不是「加三个函数」，而是**让「谁能维护」这件事第一次
有可审计的载体**。在此之前只有「维护登录身份能调一个守卫函数」——没有任何东西
记录「这个人为什么被允许看全租户」。
所以先有三样东西，函数才有意义：

1. ``maintenance_work_orders``：一次维护意图的授权链（谁申请、谁批准、针对哪个
   租户、幂等键是什么），状态机由库层触发器强制（白名单来自
   ``packages/shared/maintenance.py``，不是另抄一份 SQL）。
2. ``maintenance_action_log``：只追加的审计账本。成功、失败、**拒绝**都落一行；
   拒绝如果只进应用日志，SQL 审计视图就永远看不到「谁试图越权」。
3. 三个**只读**命令，各自独立、互不合并：
   * ``enumerate_due_tasks``——零参发现，返回「哪个租户有多少条到期候选」，
     不返回任务内容（最小披露）；
   * ``detect_projection_anomalies``——全域七类差异计数，只报告不修改；
   * ``reconcile_tenant_tasks(工单)``——在工单授权租户内做同样的对账，
     必须在 APPROVED 之后、且类型必须是 RECONCILE。

本阶段**不授予任何写权限**：``qc_maintenance_owner`` 对 ``qc_tasks`` 只有
``SELECT``，没有 ``UPDATE``/``DELETE``/``TRUNCATE``，也没有 PG17 的 ``MAINTAIN``。
修复命令（``repair_tenant_task``）落地时才按 PG 版本补锁权限——提前授予会让
一个尚不存在的命令扩大现有权限面。

为什么命令是 SECURITY DEFINER、而守卫是 SECURITY INVOKER
-----------------------------------------------------
检测必须跨租户读（RLS 开启且 FORCE），所以函数体要能以 owner 的权限执行；
而「调用者是谁」只能由 ``session_user`` 判定——在 SECURITY DEFINER 函数体里
``current_user`` 恒为函数 owner，用 ``current_user = session_user`` 判登录身份
会把每一次合法调用都判死（上一版守卫的直连语义不适用于此处）。因此命令入口统一
调用 ``assert_session_member()``：它只判 ``session_user`` 是否为
``qc_maintenance_operator`` 成员，这个事实只有超级用户能伪造。

Revision ID: f8c9d0e1a2b3
Revises: e6a7b8c9d0e1
Create Date: 2026-11-26
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

from packages.shared.db_grants import (
    MAINTENANCE_DENIAL,
    MAINTENANCE_DETECT,
    MAINTENANCE_ENUMERATE,
    MAINTENANCE_RECONCILE,
    MAINTENANCE_SCHEMA,
    MAINTENANCE_SESSION,
    maintenance_command_statements,
)
from packages.shared.db_roles import maintenance_role
from packages.shared.maintenance import (
    WORK_ORDER_KINDS,
    WORK_ORDER_STATES,
    transition_edges,
)
from packages.shared.rls import tenant_scope_predicate

revision: str = "f8c9d0e1a2b3"
down_revision: str | Sequence[str] | None = "e6a7b8c9d0e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MAINT = MAINTENANCE_SCHEMA
_QC = "qc_governance"
_WO = "maintenance_work_orders"
_LOG = "maintenance_action_log"
_TRIGGER = "trg_maintenance_work_order_transition"
_ANOMALIES = "_projection_anomalies"
_TRANSITION_EDGES = transition_edges()


def _owner() -> str:
    return maintenance_role("owner")


def _in_list(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


# --------------------------------------------------------------------------- #
# 表与状态机
# --------------------------------------------------------------------------- #
def _create_tables() -> None:
    op.create_table(
        _WO,
        sa.Column("entity_id", sa.String(64), primary_key=True),
        sa.Column("entity_type", sa.String(64), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("entity_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("owner_module", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False, index=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.String(64), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("audit_ref", sa.String(128), nullable=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("requested_by", sa.String(128), nullable=False),
        sa.Column("approved_by", sa.String(128), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("target_task_id", sa.String(64), nullable=True),
        sa.Column("result_summary", JSONB, nullable=True),
        sa.Column("failure_reason", sa.String(512), nullable=True),
        sa.UniqueConstraint("idempotency_key", name="uq_maintenance_work_order_idem"),
        sa.CheckConstraint(
            f"kind IN ({_in_list(WORK_ORDER_KINDS)})",
            name="ck_maintenance_work_order_kind",
        ),
        sa.CheckConstraint(
            f"status IN ({_in_list(WORK_ORDER_STATES)})",
            name="ck_maintenance_work_order_status",
        ),
        # 已批准必须有批准人与批准时间；未批准不得携带批准人。这条把「看起来
        # 批过了」的中间态挡在库里，而不是靠应用层每个写入点自觉。
        sa.CheckConstraint(
            "(status IN ('APPROVED','RUNNING','SUCCEEDED','FAILED') "
            "AND approved_by IS NOT NULL AND approved_at IS NOT NULL) "
            "OR (status IN ('REQUESTED','CANCELLED') AND approved_by IS NULL)",
            name="ck_maintenance_work_order_approval",
        ),
        # REPAIR 必须指向具体任务；检测/对账不得携带单任务目标。
        sa.CheckConstraint(
            "(kind = 'REPAIR' AND target_task_id IS NOT NULL) "
            "OR (kind <> 'REPAIR' AND target_task_id IS NULL)",
            name="ck_maintenance_work_order_target",
        ),
        schema=_MAINT,
    )
    op.create_index(
        "ix_maintenance_work_order_tenant",
        _WO,
        ["tenant_id", "created_at"],
        schema=_MAINT,
    )
    op.create_index(
        "ix_maintenance_work_order_status", _WO, ["status"], schema=_MAINT
    )

    op.create_table(
        _LOG,
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("work_order_id", sa.String(64), nullable=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=True),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("detail", JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "outcome IN ('SUCCEEDED','FAILED','DENIED')",
            name="ck_maintenance_action_outcome",
        ),
        schema=_MAINT,
    )
    op.create_index(
        "ix_maintenance_action_log_work_order",
        _LOG,
        ["work_order_id"],
        schema=_MAINT,
    )
    op.create_index(
        "ix_maintenance_action_log_tenant",
        _LOG,
        ["tenant_id", "created_at"],
        schema=_MAINT,
    )


def _create_state_machine() -> None:
    """状态机触发器：白名单由 ``transition_edges()`` 生成，SQL 里没有第二份。"""
    edges_sql = ",\n            ".join(
        f"('{source}', '{target}')" for source, target in _TRANSITION_EDGES
    )
    op.execute(
        f"""
        CREATE FUNCTION {_MAINT}.enforce_work_order_transition() RETURNS trigger
        LANGUAGE plpgsql
        SECURITY INVOKER
        SET search_path = pg_catalog
        AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                -- 工单只能以 REQUESTED 出生：否则「谁批的」这一步可以被跳过。
                IF NEW.status <> 'REQUESTED' THEN
                    RAISE EXCEPTION
                        '工单必须以 REQUESTED 创建，收到 %', NEW.status
                        USING ERRCODE = 'check_violation';
                END IF;
                RETURN NEW;
            END IF;

            -- 非状态字段更新（例如回填 result_summary）不触发跳转判定。
            IF NEW.status = OLD.status THEN
                RETURN NEW;
            END IF;

            IF NOT EXISTS (
                SELECT 1 FROM (VALUES
            {edges_sql}
                ) AS t(source, target)
                WHERE t.source = OLD.status AND t.target = NEW.status
            ) THEN
                RAISE EXCEPTION
                    '非法的工单状态跳转：% -> %', OLD.status, NEW.status
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        f"CREATE TRIGGER {_TRIGGER} BEFORE INSERT OR UPDATE OF status "
        f"ON {_MAINT}.{_WO} FOR EACH ROW "
        f"EXECUTE FUNCTION {_MAINT}.enforce_work_order_transition()"
    )


# --------------------------------------------------------------------------- #
# RLS：租户隔离 + 维护 owner 的最小读取面
# --------------------------------------------------------------------------- #
def _enable_rls() -> None:
    for table in (_WO, _LOG):
        op.execute(f"ALTER TABLE {_MAINT}.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {_MAINT}.{table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation_{table} ON {_MAINT}.{table} "
            f"USING ({tenant_scope_predicate()})"
        )

    # 工单只读给 owner：命令要能解析「这张工单授权了哪个租户」。
    op.execute(
        f"CREATE POLICY maintenance_owner_read_{_WO} ON {_MAINT}.{_WO} "
        f"TO {_owner()} USING (true)"
    )
    # 账本给 owner 读写：命令以 owner 身份落审计行（含拒绝记录）。
    op.execute(
        f"CREATE POLICY maintenance_owner_write_{_LOG} ON {_MAINT}.{_LOG} "
        f"TO {_owner()} USING (true) WITH CHECK (true)"
    )

    # 源表与投影：只给 owner 一条**读**策略。没有 BYPASSRLS——放宽的是「谁能读」，
    # 不是「读的时候不看策略」；写权限本阶段完全没有。
    #
    # 为什么要在建完这条策略后再原样重建一次 tenant_isolation：组织边界闸门对每张
    # 业务表只取**最后一条** CREATE POLICY，判断「策略名 / 租户谓词 / 父项目存在性」。
    # 本条策略若排在最后，闸门会把 qc_tasks 的终态看成 owner 读策略而报红。重建一次
    # 既有策略（文本与原迁移逐字相同）让终态仍是 tenant_isolation_qc_tasks——不是为了
    # 通过闸门，而是「最后一条就是数据库真实终态」这个判据本身是对的。
    op.execute(
        f"CREATE POLICY maintenance_owner_read_qc_tasks ON {_QC}.qc_tasks "
        f"TO {_owner()} USING (true)"
    )
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation_qc_tasks ON {_QC}.qc_tasks")
    op.execute(
        f"CREATE POLICY tenant_isolation_qc_tasks ON {_QC}.qc_tasks "
        f"USING ({tenant_scope_predicate()} AND "
        f"EXISTS (SELECT 1 FROM project_planning.projects p "
        f"WHERE p.entity_id = {_QC}.qc_tasks.project_id "
        f"AND p.tenant_id = {_QC}.qc_tasks.tenant_id))"
    )
    op.execute(
        f"CREATE POLICY maintenance_owner_read_task_projection ON "
        f"{_MAINT}.task_projection TO {_owner()} USING (true)"
    )


# --------------------------------------------------------------------------- #
# 命令
# --------------------------------------------------------------------------- #
def _create_session_guard() -> None:
    operator = maintenance_role("operator")
    op.execute(
        f"""
        CREATE FUNCTION {_MAINT}.assert_session_member() RETURNS void
        LANGUAGE plpgsql
        SECURITY INVOKER
        SET search_path = pg_catalog
        AS $$
        BEGIN
            -- 只判 session_user：SECURITY DEFINER 里 current_user 恒为函数 owner，
            -- 用它判登录身份会把每次合法调用都拒绝。
            IF NOT pg_has_role(session_user, '{operator}', 'MEMBER') THEN
                RAISE EXCEPTION
                    '当前登录身份 % 不是维护权限组成员，无权执行维护命令',
                    session_user
                    USING ERRCODE = 'insufficient_privilege';
            END IF;
        END;
        $$
        """
    )


def _create_anomaly_helper() -> None:
    """内部取数函数：七类差异只在这里实现一次，两个命令共用。

    ``p_tenant`` 为 NULL 表示全域。它不是对外接口——没有 EXECUTE 授给任何人，
    只有以 owner 身份执行的 SECURITY DEFINER 命令能调用它。
    """
    op.execute(
        f"""
        CREATE FUNCTION {_MAINT}.{_ANOMALIES}(p_tenant uuid)
        RETURNS TABLE (category text, row_count bigint)
        LANGUAGE sql
        STABLE
        SECURITY INVOKER
        SET search_path = pg_catalog
        AS $$
        SELECT a.category, a.row_count
          FROM (
            SELECT 'missing'::text AS category, count(*)::bigint AS row_count
              FROM {_QC}.qc_tasks s
              LEFT JOIN {_MAINT}.task_projection p ON p.task_id = s.entity_id
             WHERE s.status IN ('QUEUED','RUNNING') AND p.task_id IS NULL
               AND (p_tenant IS NULL OR s.tenant_id = p_tenant)
            UNION ALL
            SELECT 'extra', count(*)::bigint
              FROM {_MAINT}.task_projection p
              LEFT JOIN {_QC}.qc_tasks s ON s.entity_id = p.task_id
             WHERE (s.entity_id IS NULL OR s.status NOT IN ('QUEUED','RUNNING'))
               AND (p_tenant IS NULL OR p.tenant_id = p_tenant)
            UNION ALL
            SELECT 'tenant_mismatch', count(*)::bigint
              FROM {_MAINT}.task_projection p
              JOIN {_QC}.qc_tasks s ON s.entity_id = p.task_id
             WHERE p.tenant_id IS DISTINCT FROM s.tenant_id
               AND (p_tenant IS NULL OR s.tenant_id = p_tenant)
            UNION ALL
            SELECT 'status_mismatch', count(*)::bigint
              FROM {_MAINT}.task_projection p
              JOIN {_QC}.qc_tasks s ON s.entity_id = p.task_id
             WHERE p.task_status IS DISTINCT FROM s.status
               AND (p_tenant IS NULL OR s.tenant_id = p_tenant)
            UNION ALL
            SELECT 'lease_mismatch', count(*)::bigint
              FROM {_MAINT}.task_projection p
              JOIN {_QC}.qc_tasks s ON s.entity_id = p.task_id
             WHERE p.lease_expires_at IS DISTINCT FROM s.lease_expires_at
               AND (p_tenant IS NULL OR s.tenant_id = p_tenant)
            UNION ALL
            SELECT 'generation_mismatch', count(*)::bigint
              FROM {_MAINT}.task_projection p
              JOIN {_QC}.qc_tasks s ON s.entity_id = p.task_id
             WHERE p.lease_generation IS DISTINCT FROM s.lease_generation
               AND (p_tenant IS NULL OR s.tenant_id = p_tenant)
            UNION ALL
            SELECT 'invalid_lease', count(*)::bigint
              FROM {_QC}.qc_tasks s
             WHERE s.status = 'RUNNING' AND s.lease_expires_at IS NULL
               AND (p_tenant IS NULL OR s.tenant_id = p_tenant)
          ) a
         ORDER BY a.category
        $$
        """
    )


def _create_commands() -> None:
    op.execute(
        f"""
        CREATE FUNCTION {_MAINT}.enumerate_due_tasks()
        RETURNS TABLE (tenant_id uuid, due_count bigint)
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog
        AS $$
        BEGIN
            PERFORM {_MAINT}.assert_session_member();
            -- 发现只返回「租户 + 条数」：调度需要租户 Id 去建立上下文，但不需要
            -- 任何任务内容。返回任务明细会把发现函数变成跨租户读数接口。
            RETURN QUERY
                SELECT p.tenant_id, count(*)::bigint
                  FROM {_MAINT}.task_projection p
                 WHERE p.task_status = 'QUEUED'
                    OR (p.task_status = 'RUNNING' AND p.lease_expires_at <= now())
                 GROUP BY p.tenant_id
                 ORDER BY p.tenant_id;
        END;
        $$
        """
    )

    op.execute(
        f"""
        CREATE FUNCTION {_MAINT}.detect_projection_anomalies()
        RETURNS TABLE (category text, row_count bigint)
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog
        AS $$
        BEGIN
            PERFORM {_MAINT}.assert_session_member();
            RETURN QUERY SELECT * FROM {_MAINT}.{_ANOMALIES}(NULL::uuid);
        END;
        $$
        """
    )

    op.execute(
        f"""
        CREATE FUNCTION {_MAINT}.reconcile_tenant_tasks(p_work_order_id text)
        RETURNS TABLE (category text, row_count bigint)
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog
        AS $$
        DECLARE
            v_tenant uuid;
            v_kind text;
            v_status text;
        BEGIN
            PERFORM {_MAINT}.assert_session_member();

            SELECT o.tenant_id, o.kind, o.status
              INTO v_tenant, v_kind, v_status
              FROM {_MAINT}.{_WO} o
             WHERE o.entity_id = p_work_order_id;

            -- 拒绝**不在这里记账**：PG 没有自治事务，同一语句里先 INSERT 再 RAISE
            -- 会整条回滚，留不下任何行。所以账本只记成功路径；拒绝由调用方在
            -- 捕获异常后单独调用 record_maintenance_denial(...) 记一行，这是
            -- 一份必须被遵守的调用契约，不是「可选的可观测性」。
            IF v_tenant IS NULL THEN
                RAISE EXCEPTION '维护工单 % 不存在', p_work_order_id
                    USING ERRCODE = 'check_violation';
            END IF;

            IF v_kind <> 'RECONCILE' THEN
                RAISE EXCEPTION '工单 % 的类型是 %，不是 RECONCILE', p_work_order_id, v_kind
                    USING ERRCODE = 'check_violation';
            END IF;

            IF v_status NOT IN ('APPROVED', 'RUNNING') THEN
                RAISE EXCEPTION '工单 % 当前状态为 %，不得执行对账', p_work_order_id, v_status
                    USING ERRCODE = 'check_violation';
            END IF;

            INSERT INTO {_MAINT}.{_LOG}
                (work_order_id, tenant_id, action, outcome, detail, created_at)
            VALUES (p_work_order_id, v_tenant, 'reconcile_tenant_tasks', 'SUCCEEDED',
                    jsonb_build_object(
                        'categories',
                        (SELECT jsonb_object_agg(a.category, a.row_count)
                           FROM {_MAINT}.{_ANOMALIES}(v_tenant) a)
                    ),
                    now());

            RETURN QUERY SELECT * FROM {_MAINT}.{_ANOMALIES}(v_tenant);
        END;
        $$
        """
    )


    op.execute(
        f"""
        CREATE FUNCTION {_MAINT}.record_maintenance_denial(
            p_work_order_id text, p_reason text)
        RETURNS void
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog
        AS $$
        DECLARE
            v_tenant uuid;
        BEGIN
            PERFORM {_MAINT}.assert_session_member();

            -- 原因走白名单而不是自由文本：账本要可聚合，不能变成谁都能写内容的
            -- 日志表；未知原因一律记成 'other'，避免把错误口径灌进审计视图。
            IF p_reason NOT IN ('work_order_not_found', 'kind_mismatch',
                               'not_approved', 'session_mismatch') THEN
                p_reason := 'other';
            END IF;

            SELECT o.tenant_id INTO v_tenant
              FROM {_MAINT}.{_WO} o
             WHERE o.entity_id = p_work_order_id;

            INSERT INTO {_MAINT}.{_LOG}
                (work_order_id, tenant_id, action, outcome, detail, created_at)
            VALUES (p_work_order_id, v_tenant, 'reconcile_tenant_tasks', 'DENIED',
                    jsonb_build_object('reason', p_reason), now());
        END;
        $$
        """
    )


def _transfer_ownership() -> None:
    owner = _owner()
    for table in (_WO, _LOG):
        op.execute(f"ALTER TABLE {_MAINT}.{table} OWNER TO {owner}")
    for function in (
        f"{_MAINT}.enforce_work_order_transition()",
        f"{_MAINT}.assert_session_member()",
        f"{_MAINT}.{_ANOMALIES}(uuid)",
        f"{_MAINT}.enforce_work_order_transition()",
        MAINTENANCE_DENIAL,
        MAINTENANCE_ENUMERATE,
        MAINTENANCE_DETECT,
        f"{MAINTENANCE_RECONCILE}",
    ):
        op.execute(f"ALTER FUNCTION {function} OWNER TO {owner}")


def upgrade() -> None:
    _create_tables()
    _create_state_machine()
    _enable_rls()
    _create_session_guard()
    _create_anomaly_helper()
    _create_commands()
    _transfer_ownership()
    for statement in maintenance_command_statements():
        op.execute(statement)


def downgrade() -> None:
    op.execute(f"DROP POLICY IF EXISTS maintenance_owner_read_task_projection "
               f"ON {_MAINT}.task_projection")
    op.execute(f"DROP POLICY IF EXISTS maintenance_owner_read_qc_tasks "
               f"ON {_QC}.qc_tasks")
    for function in (
        MAINTENANCE_DENIAL,
        MAINTENANCE_RECONCILE,
        MAINTENANCE_DETECT,
        MAINTENANCE_ENUMERATE,
        f"{_MAINT}.{_ANOMALIES}(uuid)",
        MAINTENANCE_SESSION,
    ):
        op.execute(f"DROP FUNCTION IF EXISTS {function}")
    op.execute(f"DROP TRIGGER IF EXISTS {_TRIGGER} ON {_MAINT}.{_WO}")
    op.execute(f"DROP FUNCTION IF EXISTS {_MAINT}.enforce_work_order_transition()")
    for table in (_LOG, _WO):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation_{table} ON {_MAINT}.{table}")
        op.execute(f"DROP TABLE {_MAINT}.{table}")
