"""调度发现身份：``qc_scheduler`` 只能调用零参发现函数（WP-B）。

为什么需要单独一个角色
----------------------
WP-A 之后维护身份已经能「全域发现 + 租户内修复」，但把这两件事塞进同一个
可登录身份，等于承认「调度进程必须持有修复能力」。调度是常驻、高频、暴露面
最大的那个进程；修复是低频、必须挂工单的人工动作。权限必须按**进程职责**切，
而不是按「省一个角色」。

所以本迁移新增一个只做发现的登录身份：

* 唯一可达路径是 ``qc_maintenance.enumerate_due_tasks()``——SECURITY DEFINER，
  返回「租户 + 待检条数」，不含任何任务内容；
* 对 ``qc_tasks`` / 投影**没有**任何表权限，也不进入维护 operator 组；
* 维护命令（对账 / 检测 / 修复 / 补记）对它逐个 ``REVOKE``，这样即便将来有人
  手工把它加进 ``operator``，收敛语句也能把它拉回声明状态。

发现与领取的分工
----------------
发现只回答「哪些租户现在有活」。领取、心跳、判定仍由 QC 池在**租户上下文**
里读 ``qc_tasks`` 完成：RLS 的租户隔离不因为「已经知道租户 id」而放宽，知道
租户不等于被授权读该租户的业务数据。

Revision ID: a5b6c7d8e9f0
Revises: b4c5d6e7f8a9
Create Date: 2026-11-26
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

from packages.shared.db_grants import (
    MAINTENANCE_SCHEMA,
    discovery_guard_statements,
    maintenance_function_public_reclaim_restore_statements,
    maintenance_function_public_reclaim_statements,
    scheduler_restore_statements,
    scheduler_statements,
)
from packages.shared.db_roles import maintenance_role, scheduler_role
from packages.shared.migration_identity import assert_role_present

revision: str = "a5b6c7d8e9f0"
down_revision: str | Sequence[str] | None = "b4c5d6e7f8a9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NO_ESCALATION = "NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS"
_DISCOVERY_GUARD = "assert_discovery_identity"

# 发现函数的两份定义：新身份（operator 成员 **或** scheduler）与回滚时恢复的
# 原口径（仅 operator 成员）。两份放在一处，避免「正向改了、回滚忘了改」。
_ENUMERATE_WITH_DISCOVERY = f"""
CREATE OR REPLACE FUNCTION {MAINTENANCE_SCHEMA}.enumerate_due_tasks()
RETURNS TABLE (tenant_id uuid, due_count bigint)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
BEGIN
    PERFORM {MAINTENANCE_SCHEMA}.{_DISCOVERY_GUARD}();
    -- 发现只返回「租户 + 条数」：调度需要租户 Id 去建立上下文，但不需要
    -- 任何任务内容。返回任务明细会把发现函数变成跨租户读数接口。
    RETURN QUERY
        SELECT p.tenant_id, count(*)::bigint
          FROM {MAINTENANCE_SCHEMA}.task_projection p
         WHERE p.task_status = 'QUEUED'
            OR (p.task_status = 'RUNNING' AND p.lease_expires_at <= now())
         GROUP BY p.tenant_id
         ORDER BY p.tenant_id;
END;
$$
"""

_ENUMERATE_WITHOUT_DISCOVERY = f"""
CREATE OR REPLACE FUNCTION {MAINTENANCE_SCHEMA}.enumerate_due_tasks()
RETURNS TABLE (tenant_id uuid, due_count bigint)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
BEGIN
    PERFORM {MAINTENANCE_SCHEMA}.assert_session_member();
    RETURN QUERY
        SELECT p.tenant_id, count(*)::bigint
          FROM {MAINTENANCE_SCHEMA}.task_projection p
         WHERE p.task_status = 'QUEUED'
            OR (p.task_status = 'RUNNING' AND p.lease_expires_at <= now())
         GROUP BY p.tenant_id
         ORDER BY p.tenant_id;
END;
$$
"""


def _create_discovery_guard() -> None:
    """发现守卫：允许调度发现身份，或维护权限组成员。

    为什么不直接 ``GRANT operator TO qc_scheduler``：角色成员是**权限继承**，
    而 PostgreSQL 的 EXECUTE 检查按函数生效——把 scheduler 加进 operator，
    就等于把 ``repair_tenant_task`` 一起继承过来，再对 scheduler ``REVOKE``
    也挡不住继承路径。所以「只发现」必须是**另一个守卫**，而不是同一条守卫加人。
    """
    operator = maintenance_role("operator")
    scheduler = scheduler_role()
    op.execute(
        f"""
        CREATE FUNCTION {MAINTENANCE_SCHEMA}.{_DISCOVERY_GUARD}() RETURNS void
        LANGUAGE plpgsql
        SECURITY INVOKER
        SET search_path = pg_catalog
        AS $$
        BEGIN
            IF NOT pg_has_role(session_user, '{operator}', 'MEMBER')
               AND session_user <> '{scheduler}' THEN
                RAISE EXCEPTION
                    '当前身份 % 既不是维护权限组成员，也不是调度发现身份', session_user
                    USING ERRCODE = 'insufficient_privilege';
            END IF;
        END;
        $$
        """
    )
    op.execute(
        f"ALTER FUNCTION {MAINTENANCE_SCHEMA}.{_DISCOVERY_GUARD}() "
        f"OWNER TO {maintenance_role('owner')}"
    )


def _assert_scheduler_role() -> None:
    """断言 Scheduler 登录角色已由 bootstrap 创建（角色归身份治理，不归迁移）。

    D4 裁决（Owner 2026-11-28）：基础角色的创建、属性与凭据由 Bootstrap Identity
    统一负责；迁移只断言存在、管理自己的对象与授权。原实现每次 upgrade 都
    ``CREATE/ALTER ROLE`` + 写口令，使迁移成为第二份身份事实源，也让降级必须
    删角色才能对称——而角色是集群级对象，删不掉就整链失败。
    """
    assert_role_present(scheduler_role(), op=op)


def upgrade() -> None:
    _assert_scheduler_role()
    _create_discovery_guard()
    op.execute(_ENUMERATE_WITH_DISCOVERY)
    for statement in discovery_guard_statements():
        op.execute(statement)
    for statement in maintenance_function_public_reclaim_statements():
        op.execute(statement)
    for statement in scheduler_statements():
        op.execute(statement)


def downgrade() -> None:
    for statement in scheduler_restore_statements():
        op.execute(statement)
    for statement in maintenance_function_public_reclaim_restore_statements():
        op.execute(statement)
    op.execute(_ENUMERATE_WITHOUT_DISCOVERY)
    op.execute(
        f"DROP FUNCTION IF EXISTS {MAINTENANCE_SCHEMA}.{_DISCOVERY_GUARD}()"
    )
    # 角色不删：downgrade 是「回到上一版结构」，不是「删掉部署方可能已在用的
    # 登录身份」——删角色会连带清掉成员关系与口令，属于数据丢失。而且角色是
    # **集群级**对象：本库收干净了，别的库还授着权限，``DROP ROLE`` 照样失败，
    # 只会在「多库都跑过迁移」的环境里炸出一个与结构无关的错误。
    # 需要彻底撤销时按正式治理流程单独执行（先确认无人持有该身份）。
