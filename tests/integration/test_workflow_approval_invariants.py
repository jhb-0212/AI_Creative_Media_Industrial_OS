"""审批引擎的结构性约束（绕过服务层，直接对数据库动手）。

领域层拒绝非法构造还不够：一次手工补数据、一次迁移脚本、一次其它服务直连
写入都可能留下不可信的审批事实。本文件证明六条性质：

1. flows / steps 是 append-only：UPDATE / DELETE 被触发器拒绝（超级用户也一样）；
2. 票据只开放状态机列：应用角色不能改 flow_id / created_by 等事实列；
3. Agent 投票人、空理由否决、契约外取值被 CHECK 拒绝；
4. 一组一人一票由唯一索引兜底（并发重复投票不会落两票）；
5. 票据终态必须有 completed_at（结论时间不可缺失）；
6. 三张表都启用 RLS：跨租户、跨项目读不到。
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from packages.shared.database import build_migration_url
from packages.shared.db_roles import DatabaseDomain, all_business_roles, role_for
from tests.conftest import apply_db_context

_TENANT = uuid.UUID("00000000-0000-0000-0000-00000000d5c1")
_OTHER_TENANT = uuid.UUID("00000000-0000-0000-0000-00000000d5c2")
_PROJECT = "proj-approval-guard"
_OTHER_PROJECT = "proj-approval-other"

pytestmark = pytest.mark.asyncio


@pytest.fixture(scope="module")
async def admin_engine() -> AsyncEngine:
    """迁移角色（超级用户）连库：证明触发器 / CHECK 对超级用户同样生效。"""
    engine = create_async_engine(build_migration_url(), pool_pre_ping=True)
    try:
        yield engine
    finally:
        await engine.dispose()


async def _insert_flow(
    admin_engine: AsyncEngine, *, entity_id: str | None = None
) -> str:
    entity_id = entity_id or f"flow-guard-{uuid.uuid4().hex[:12]}"
    async with admin_engine.begin() as conn:
        await conn.execute(
            sa.text(
                """
                INSERT INTO workflow.approval_flows (
                    entity_id, entity_type, schema_version, entity_version,
                    status, owner_module, tenant_id, organization_id,
                    created_at, created_by, updated_at, audit_ref,
                    action_key, resource_type, resource_id, project_id,
                    mode, groups, ruleset_version
                ) VALUES (
                    :eid, 'ApprovalFlow', 205, 1,
                    'ACTIVE', 'workflow', :tid, :tid,
                    now(), 'guard', now(), NULL,
                    'DELIVERY.MANIFEST.APPROVE', 'DeliveryManifest', 'm-1', :pid,
                    'ALL',
                    '[{"name":"delivery_owner","roles":["交付负责人"]}]'::jsonb,
                    3
                )
                """
            ),
            {"eid": entity_id, "tid": _TENANT, "pid": _PROJECT},
        )
    return entity_id


async def _insert_ticket(
    admin_engine: AsyncEngine,
    *,
    entity_id: str | None = None,
    flow_id: str | None = None,
    status: str = "PENDING",
    completed: bool = False,
) -> str:
    entity_id = entity_id or f"ticket-guard-{uuid.uuid4().hex[:12]}"
    flow_id = flow_id or f"flow-{uuid.uuid4().hex[:12]}"
    completed_at = "now()" if completed else "NULL"
    async with admin_engine.begin() as conn:
        await conn.execute(
            sa.text(
                f"""
                INSERT INTO workflow.approval_tickets (
                    entity_id, entity_type, schema_version, entity_version,
                    status, owner_module, tenant_id, organization_id,
                    created_at, created_by, updated_at, audit_ref,
                    flow_id, project_id, version, completed_at
                ) VALUES (
                    :eid, 'ApprovalTicket', 205, 1,
                    :status, 'workflow', :tid, :tid,
                    now(), 'guard', now(), NULL,
                    :fid, :pid, 1, {completed_at}
                )
                """
            ),
            {
                "eid": entity_id,
                "fid": flow_id,
                "status": status,
                "tid": _TENANT,
                "pid": _PROJECT,
            },
        )
    return entity_id


async def _insert_step(
    admin_engine: AsyncEngine,
    *,
    ticket_id: str,
    group: str = "delivery_owner",
    voter: str = "voter-guard",
    subject_type: str = "human",
    decision: str = "APPROVE",
    reason: str = "同意",
) -> str:
    entity_id = f"step-guard-{uuid.uuid4().hex[:12]}"
    async with admin_engine.begin() as conn:
        await conn.execute(
            sa.text(
                """
                INSERT INTO workflow.approval_steps (
                    entity_id, entity_type, schema_version, entity_version,
                    status, owner_module, tenant_id, organization_id,
                    created_at, created_by, updated_at, audit_ref,
                    ticket_id, group_name, voter_subject_id, voter_subject_type,
                    decision, reason, decided_at, project_id
                ) VALUES (
                    :eid, 'ApprovalStep', 205, 1,
                    'RECORDED', 'workflow', :tid, :tid,
                    now(), 'guard', now(), NULL,
                    :ticket, :group, :voter, :stype,
                    :decision, :reason, now(), :pid
                )
                """
            ),
            {
                "eid": entity_id,
                "ticket": ticket_id,
                "group": group,
                "voter": voter,
                "stype": subject_type,
                "decision": decision,
                "reason": reason,
                "tid": _TENANT,
                "pid": _PROJECT,
            },
        )
    return entity_id


# --- append-only 事实 --------------------------------------------------------


@pytest.mark.parametrize(
    ("table", "mutation"),
    [
        ("approval_flows", "UPDATE workflow.approval_flows SET action_key = 'X' WHERE entity_id = :e"),
        ("approval_flows", "DELETE FROM workflow.approval_flows WHERE entity_id = :e"),
        ("approval_steps", "UPDATE workflow.approval_steps SET reason = 'X' WHERE entity_id = :e"),
        ("approval_steps", "DELETE FROM workflow.approval_steps WHERE entity_id = :e"),
    ],
)
async def test_facts_are_append_only_even_for_superuser(
    admin_engine: AsyncEngine, table: str, mutation: str
) -> None:
    """流程配置与选票不可改写：超级用户直连也拦，证明拦的是结构而不是角色。"""
    if table == "approval_flows":
        entity_id = await _insert_flow(admin_engine)
    else:
        ticket_id = await _insert_ticket(admin_engine)
        entity_id = await _insert_step(admin_engine, ticket_id=ticket_id)
    with pytest.raises(DBAPIError) as exc_info:
        async with admin_engine.begin() as conn:
            await conn.execute(sa.text(mutation), {"e": entity_id})
    assert "append-only" in str(exc_info.value)


# --- CHECK 约束 --------------------------------------------------------------


async def test_agent_voter_rejected_by_check(admin_engine: AsyncEngine) -> None:
    """Agent 不得投票：绕过所有应用代码仍被数据库拒绝。"""
    ticket_id = await _insert_ticket(admin_engine)
    with pytest.raises(DBAPIError) as exc_info:
        await _insert_step(admin_engine, ticket_id=ticket_id, subject_type="agent")
    assert "no_agent_voter" in str(exc_info.value)


async def test_reject_without_reason_rejected_by_check(
    admin_engine: AsyncEngine,
) -> None:
    """否决必须有理由：数据库 CHECK 兜住手工写入。"""
    ticket_id = await _insert_ticket(admin_engine)
    with pytest.raises(DBAPIError) as exc_info:
        await _insert_step(
            admin_engine,
            ticket_id=ticket_id,
            decision="REJECT",
            reason="  ",
        )
    assert "reject_needs_reason" in str(exc_info.value)


async def test_contract_outside_values_rejected(admin_engine: AsyncEngine) -> None:
    """契约外枚举进不了库（可表示 ≠ 可进入）。"""
    with pytest.raises(DBAPIError) as exc_info:
        await _insert_ticket(admin_engine, status="BANANA")
    assert "status_registered" in str(exc_info.value)

    ticket_id = await _insert_ticket(admin_engine)
    with pytest.raises(DBAPIError) as exc_info:
        await _insert_step(admin_engine, ticket_id=ticket_id, decision="MAYBE")
    assert "decision_registered" in str(exc_info.value)


async def test_terminal_ticket_without_completed_at_rejected(
    admin_engine: AsyncEngine,
) -> None:
    """终态票据必须有结论时间。"""
    with pytest.raises(DBAPIError) as exc_info:
        await _insert_ticket(admin_engine, status="APPROVED", completed=False)
    assert "terminal_has_time" in str(exc_info.value)


# --- 唯一索引 ----------------------------------------------------------------


async def test_one_vote_per_group_per_voter(admin_engine: AsyncEngine) -> None:
    """同一分组同一投票人只能一票；重复提交由唯一索引兜住。"""
    ticket_id = await _insert_ticket(admin_engine)
    await _insert_step(admin_engine, ticket_id=ticket_id, voter="same-voter")
    with pytest.raises(DBAPIError) as exc_info:
        await _insert_step(admin_engine, ticket_id=ticket_id, voter="same-voter")
    assert "one_vote_per_group" in str(exc_info.value)


async def test_same_voter_can_vote_in_different_groups(admin_engine: AsyncEngine) -> None:
    """唯一键是 (票据,分组,投票人)：跨分组不冲突。"""
    ticket_id = await _insert_ticket(admin_engine)
    await _insert_step(admin_engine, ticket_id=ticket_id, voter="same-voter", group="delivery_owner")
    await _insert_step(admin_engine, ticket_id=ticket_id, voter="same-voter", group="qc_rights")


# --- 列级授权 ----------------------------------------------------------------


async def test_fact_tables_are_insert_only_for_app_roles(
    admin_engine: AsyncEngine,
) -> None:
    """系统目录断言：flows / steps 应用角色只有 SELECT/INSERT。"""
    async with admin_engine.connect() as conn:
        rows = (
            await conn.execute(
                sa.text(
                    """
                    SELECT grantee, table_name, privilege_type
                    FROM information_schema.table_privileges
                    WHERE table_schema = 'workflow'
                      AND table_name IN ('approval_flows', 'approval_steps')
                      AND privilege_type IN ('INSERT', 'UPDATE', 'DELETE')
                    """
                )
            )
        ).all()
    workflow_role = role_for(DatabaseDomain.WORKFLOW)
    business_roles = set(all_business_roles())
    writers = {
        (row.grantee, row.table_name, row.privilege_type)
        for row in rows
        if row.grantee in business_roles
    }
    assert writers == {
        (workflow_role, "approval_flows", "INSERT"),
        (workflow_role, "approval_steps", "INSERT"),
    }


async def test_ticket_update_is_column_scoped(admin_engine: AsyncEngine) -> None:
    """票据 UPDATE 只授状态机列；事实列（flow_id/created_by）不可改。"""
    async with admin_engine.connect() as conn:
        rows = (
            await conn.execute(
                sa.text(
                    """
                    SELECT column_name, privilege_type
                    FROM information_schema.column_privileges
                    WHERE table_schema = 'workflow'
                      AND table_name = 'approval_tickets'
                      AND grantee = :role
                      AND privilege_type = 'UPDATE'
                    """
                ),
                {"role": role_for(DatabaseDomain.WORKFLOW)},
            )
        ).all()
    updatable = {row.column_name for row in rows}
    assert updatable == {"status", "version", "completed_at", "updated_at", "audit_ref"}
    assert "flow_id" not in updatable
    assert "created_by" not in updatable


# --- RLS ---------------------------------------------------------------------


@pytest.mark.parametrize("table", ["approval_flows", "approval_tickets", "approval_steps"])
async def test_rls_blocks_other_tenant_and_other_project(
    admin_engine: AsyncEngine,
    workflow_sm: async_sessionmaker[AsyncSession],
    table: str,
) -> None:
    """三张表都做租户 + 项目隔离（用应用角色会话，超级用户会假绿）。"""
    ticket_id = await _insert_ticket(admin_engine)
    if table == "approval_flows":
        entity_id = await _insert_flow(admin_engine)
    elif table == "approval_steps":
        entity_id = await _insert_step(admin_engine, ticket_id=ticket_id)
    else:
        entity_id = ticket_id

    async def _count(*, tenant_id: uuid.UUID, projects: tuple[str, ...]) -> int:
        async with workflow_sm() as session:
            await apply_db_context(session, tenant_id=tenant_id, project_ids=projects)
            return int(
                (
                    await session.execute(
                        sa.text(f"SELECT count(*) FROM workflow.{table} WHERE entity_id = :e"),
                        {"e": entity_id},
                    )
                ).scalar_one()
            )

    visible = await _count(tenant_id=_TENANT, projects=(_PROJECT,))
    other_tenant = await _count(tenant_id=_OTHER_TENANT, projects=(_PROJECT,))
    other_project = await _count(tenant_id=_TENANT, projects=(_OTHER_PROJECT,))
    assert (visible, other_tenant, other_project) == (1, 0, 0)
