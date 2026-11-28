"""修复命令：授权范围内的单任务原子投影重建（WP-A.2）。

这是 WP-A 里**唯一会写数据**的命令，因此它比三条只读命令多了四道约束，每一道
都对应一类真实失效：

1. **工单授权**：必须是有 ``target_task_id`` 的 ``REPAIR`` 工单、已批准、且目标
   正是本次要修的任务。没有工单就不能修——否则维护身份就是一个万能写入口。
2. **租户一致**：任务自身的 ``tenant_id`` 必须等于工单租户；跨租户修复一律拒绝。
3. **固定锁序 + 锁内读**：先在 ``qc_tasks`` 取 ``SHARE ROW EXCLUSIVE``，再锁
   ``task_projection``，然后才读源行。锁在**读之前**取得，持锁到事务结束——
   先读后锁在本事务里仍可能拿到旧快照（``READ COMMITTED`` 下锁只挡别人的新写，
   不会重读已读过的行）。
4. **原子**：锁、读、重建、审计同一事务，任一步失败整体回滚，不存在
   「投影改了一半」的中间态。

为什么不在一张表上批量修：一次调用只修一个任务，配合事务内
``lock_timeout``/``statement_timeout``，把持锁时间与影响面都钉死在最小单位。
批量由调用方按任务逐次发起，而不是让一个长事务握着表锁做全量重建。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from packages.shared.db_grants import (
    MAINTENANCE_REPAIR,
    maintenance_repair_restore_statements,
    maintenance_repair_statements,
)
from packages.shared.db_roles import maintenance_role

revision: str = "b4c5d6e7f8a9"
down_revision: str | Sequence[str] | None = "f8c9d0e1a2b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MAINT = "qc_maintenance"
_QC = "qc_governance"
_WO = "maintenance_work_orders"
_LOG = "maintenance_action_log"

_REPAIR_SQL = f"""
CREATE FUNCTION {_MAINT}.repair_tenant_task(
    p_work_order_id text, p_task_id text, p_attempt_id text)
RETURNS TABLE (outcome text, rows_affected integer)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    v_tenant uuid;
    v_kind text;
    v_status text;
    v_target text;
    v_task_id text;
    v_task_tenant uuid;
    v_project text;
    v_task_status text;
    v_created_at timestamp with time zone;
    v_lease_expires_at timestamp with time zone;
    v_generation bigint;
    v_version integer;
    v_rows integer := 0;
BEGIN
    PERFORM {_MAINT}.assert_session_member();

    IF p_attempt_id IS NULL OR btrim(p_attempt_id) = '' THEN
        RAISE EXCEPTION '修复必须携带 attempt_id'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    -- 幂等：同一次尝试已入账（成功或失败）就直接返回，不再动数据。
    IF EXISTS (SELECT 1 FROM {_MAINT}.{_LOG} l
                WHERE l.attempt_id = p_attempt_id) THEN
        RETURN QUERY SELECT 'already_applied'::text, 0;
        RETURN;
    END IF;

    SELECT o.tenant_id, o.kind, o.status, o.target_task_id
      INTO v_tenant, v_kind, v_status, v_target
      FROM {_MAINT}.{_WO} o
     WHERE o.entity_id = p_work_order_id;

    IF v_tenant IS NULL THEN
        RAISE EXCEPTION '维护工单 % 不存在', p_work_order_id
            USING ERRCODE = 'check_violation';
    END IF;
    IF v_kind <> 'REPAIR' THEN
        RAISE EXCEPTION '工单 % 的类型是 %，不是 REPAIR', p_work_order_id, v_kind
            USING ERRCODE = 'check_violation';
    END IF;
    IF v_status NOT IN ('APPROVED', 'RUNNING') THEN
        RAISE EXCEPTION '工单 % 当前状态为 %，不得执行修复', p_work_order_id, v_status
            USING ERRCODE = 'check_violation';
    END IF;
    IF v_target IS DISTINCT FROM p_task_id THEN
        RAISE EXCEPTION '工单 % 未授权修复任务 %', p_work_order_id, p_task_id
            USING ERRCODE = 'check_violation';
    END IF;

    -- 锁：等锁有上限，避免一次争议把维护进程挂死；持锁期间不做事，只读+重建。
    PERFORM set_config('lock_timeout', '5s', true);
    PERFORM set_config('statement_timeout', '10s', true);

    -- 固定锁序：源表 → 投影。SHARE ROW EXCLUSIVE 会挡普通 INSERT/UPDATE/DELETE，
    -- 因此在「读源行 → 重建投影」之间源数据不会变；它也让同类修复彼此串行。
    LOCK TABLE {_QC}.qc_tasks IN SHARE ROW EXCLUSIVE MODE;
    LOCK TABLE {_MAINT}.task_projection IN SHARE ROW EXCLUSIVE MODE;

    SELECT t.entity_id, t.tenant_id, t.project_id, t.status, t.created_at,
           t.lease_expires_at, t.lease_generation, t.entity_version
      INTO v_task_id, v_task_tenant, v_project, v_task_status, v_created_at,
           v_lease_expires_at, v_generation, v_version
      FROM {_QC}.qc_tasks t
     WHERE t.entity_id = p_task_id;

    IF NOT FOUND THEN
        -- 源行不存在：投影里若还留着它，就是一条永远不会被调度的幽灵候选。
        DELETE FROM {_MAINT}.task_projection p WHERE p.task_id = p_task_id;
        GET DIAGNOSTICS v_rows = ROW_COUNT;
        INSERT INTO {_MAINT}.{_LOG}
            (work_order_id, tenant_id, attempt_id, action, outcome, detail, created_at)
        VALUES (p_work_order_id, v_tenant, p_attempt_id, 'repair_tenant_task',
                'SUCCEEDED',
                jsonb_build_object('action', 'prune', 'task_id', p_task_id,
                                   'rows_affected', v_rows, 'actor', session_user),
                now());
        RETURN QUERY SELECT 'pruned'::text, v_rows;
        RETURN;
    END IF;

    IF v_task_tenant <> v_tenant THEN
        RAISE EXCEPTION '任务 % 属于租户 %，不在工单授权范围内',
            p_task_id, v_task_tenant
            USING ERRCODE = 'check_violation';
    END IF;

    IF v_task_status NOT IN ('QUEUED', 'RUNNING') THEN
        -- 终态任务不该出现在投影里：保留会让调度反复看到它。
        DELETE FROM {_MAINT}.task_projection p WHERE p.task_id = p_task_id;
        GET DIAGNOSTICS v_rows = ROW_COUNT;
        INSERT INTO {_MAINT}.{_LOG}
            (work_order_id, tenant_id, attempt_id, action, outcome, detail, created_at)
        VALUES (p_work_order_id, v_tenant, p_attempt_id, 'repair_tenant_task',
                'SUCCEEDED',
                jsonb_build_object('action', 'retire', 'task_id', p_task_id,
                                   'rows_affected', v_rows, 'actor', session_user),
                now());
        RETURN QUERY SELECT 'retired'::text, v_rows;
        RETURN;
    END IF;

    -- next_action_at 的唯一导出口径：QUEUED 立即（入队时间），RUNNING 等租约到期。
    -- 这里不引入第二个时间源，投影与任务服务写出的行必须逐字段相同。
    INSERT INTO {_MAINT}.task_projection
        (task_id, tenant_id, project_id, task_status, next_action_at,
         lease_expires_at, lease_generation, entity_version, updated_at)
    VALUES (p_task_id, v_task_tenant, v_project, v_task_status,
            CASE WHEN v_task_status = 'QUEUED' THEN v_created_at
                 ELSE v_lease_expires_at END,
            v_lease_expires_at, v_generation, v_version, now())
    ON CONFLICT (task_id) DO UPDATE SET
        tenant_id = EXCLUDED.tenant_id,
        project_id = EXCLUDED.project_id,
        task_status = EXCLUDED.task_status,
        next_action_at = EXCLUDED.next_action_at,
        lease_expires_at = EXCLUDED.lease_expires_at,
        lease_generation = EXCLUDED.lease_generation,
        entity_version = EXCLUDED.entity_version,
        updated_at = now();

    GET DIAGNOSTICS v_rows = ROW_COUNT;
    INSERT INTO {_MAINT}.{_LOG}
        (work_order_id, tenant_id, attempt_id, action, outcome, detail, created_at)
    VALUES (p_work_order_id, v_tenant, p_attempt_id, 'repair_tenant_task',
            'SUCCEEDED',
            jsonb_build_object('action', 'upsert', 'task_id', p_task_id,
                               'task_status', v_task_status,
                               'lease_generation', v_generation,
                               'rows_affected', v_rows, 'actor', session_user),
            now());

    RETURN QUERY SELECT 'repaired'::text, v_rows;
END;
$$;
"""


def _server_version_num() -> int:
    """取当前库的 ``server_version_num``；回放（无真实连接）时返回最低支持版本。

    版本决定锁权限（PG17+ ``MAINTAIN`` / PG16 ``UPDATE``）。回放拿不到活库，
    取最低支持版本与闸门的期望集合同源；两个分支的内容各有一条单元测试钉住，
    真实库上的 ACL 由 PG16 / PG18 的集成验收分别核对。
    """
    row = op.get_bind().execute(sa.text("SELECT current_setting('server_version_num')")).first()
    return int(row[0]) if row is not None else 160000


def upgrade() -> None:
    op.execute(_REPAIR_SQL)
    op.execute(f"ALTER FUNCTION {MAINTENANCE_REPAIR} OWNER TO {maintenance_role('owner')}")
    for statement in maintenance_repair_statements(_server_version_num()):
        op.execute(statement)


def downgrade() -> None:
    for statement in maintenance_repair_restore_statements(_server_version_num()):
        op.execute(statement)
    op.execute(f"DROP FUNCTION IF EXISTS {MAINTENANCE_REPAIR}")
