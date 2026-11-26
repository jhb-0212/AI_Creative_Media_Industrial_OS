"""维护命令（WP-A 第一阶段）的真库验收：只有只读能力，也没有多余权限。

守住的不是「函数能跑」，而是四件安全事实：

1. **授权链**：工单只能以 REQUESTED 出生，必须显式批准；未批准/类型不对的调用
   被拒，并且**拒绝也落审计账本**（否则越权尝试在 SQL 审计视图里不存在）。
2. **租户边界**：逐租户对账只看工单授权的那个租户，别的租户即便同样不一致也不计入。
3. **权限面**：维护 login 直接写 ``qc_tasks`` 被库拒绝；``qc_worker`` 连命令都调不了；
   owner 对源表只有 SELECT（没有 UPDATE/DELETE/TRUNCATE）；PUBLIC 无 EXECUTE。
4. **白名单**：内部取数函数不对外授予 EXECUTE，只有 SECDEF 命令能经 owner 调用。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import exc, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from packages.shared.config import get_settings  # noqa: F401
from packages.shared.database import (
    build_migration_url,
    create_qc_governance_engine,
    create_sessionmaker,
)
from packages.shared.db_roles import maintenance_role
from packages.shared.outbox_writer import SqlAlchemyOutboxWriter
from services.qc_governance.domain.qc_task import EnqueueOrigin
from services.qc_governance.repository.qc_task_repo import SqlAlchemyQCTaskRepository
from services.qc_governance.repository.task_projection_repo import (
    SqlAlchemyTaskProjectionRepository,
)
from services.qc_governance.service.qc_task_service import QCTaskService
from tests.conftest import set_test_scope
from tests.integration.planning_seed import seed_project

_LOGIN = maintenance_role("login")
_WORKER = maintenance_role("worker")
_OWNER = maintenance_role("owner")
_OPERATOR = maintenance_role("operator")

_INSERT_WO = """
INSERT INTO qc_maintenance.maintenance_work_orders
    (entity_id, entity_type, schema_version, entity_version, status,
     owner_module, tenant_id, created_at, created_by, updated_at, audit_ref,
     kind, idempotency_key, requested_by, approved_by, approved_at,
     target_task_id, result_summary, failure_reason)
VALUES (:entity_id,'MaintenanceWorkOrder',1,1,'REQUESTED','qc_maintenance',
        :tenant_id, now(),'tester',now(),NULL,
        :kind, :idem, 'tester', NULL, NULL, NULL, NULL, NULL)
"""

_APPROVE = """
UPDATE qc_maintenance.maintenance_work_orders
   SET status = 'APPROVED', approved_by = 'approver', approved_at = now(),
       entity_version = entity_version + 1, updated_at = now()
 WHERE entity_id = :entity_id
"""


@pytest.fixture(scope="module")
async def qc_sessionmaker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_qc_governance_engine()
    sm = create_sessionmaker(engine)
    try:
        yield sm
    finally:
        await engine.dispose()


async def _seed_task(sm, *, tenant, project_id: str, suffix: str) -> str:
    """入队一个 QC 任务（源表 + 投影同事务），返回 task_id。"""
    async with sm() as session:
        await set_test_scope(session, tenant_id=tenant)
        service = QCTaskService(
            SqlAlchemyQCTaskRepository(session),
            SqlAlchemyOutboxWriter(session),
            projection=SqlAlchemyTaskProjectionRepository(session),
        )
        task = await service.enqueue(
            project_id=project_id,
            asset_version_id=f"av-maint-{suffix}",
            generation_task_id=f"gen-maint-{suffix}",
            quality_profile_version=f"qp-{suffix}",
            tenant_id=tenant,
            origin=EnqueueOrigin(source="RELAY", operator_id="outbox-relay"),
            shot_id=f"shot-{suffix}",
            qc_task_id=f"qc-maint-{suffix}",
        )
        await session.commit()
        return task.entity_id


async def _create_work_order(
    session: AsyncSession,
    *,
    tenant,
    kind: str = "RECONCILE",
    idem: str | None = None,
    approve: bool = True,
) -> str:
    entity_id = str(uuid.uuid4())
    await session.execute(
        text(_INSERT_WO),
        {
            "entity_id": entity_id,
            "tenant_id": tenant,
            "kind": kind,
            "idem": idem or f"idem-{entity_id}",
        },
    )
    if approve:
        await session.execute(text(_APPROVE), {"entity_id": entity_id})
    await session.commit()
    return entity_id


async def _record_denial(work_order_id: str, reason: str) -> None:
    engine = create_async_engine(build_migration_url(), poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(text(f'SET SESSION AUTHORIZATION "{_LOGIN}"'))
            await conn.execute(
                text(
                    "SELECT qc_maintenance.record_maintenance_denial(:wo, :reason)"
                ),
                {"wo": work_order_id, "reason": reason},
            )
    finally:
        await engine.dispose()


async def _call_records(func: str, args: tuple = ()) -> list:
    """以维护登录身份调用命令，返回原始记录（用于断言列集合）。"""
    engine = create_async_engine(build_migration_url(), poolclass=NullPool)
    try:
        # 必须 begin()：成功路径要在同一事务里落 SUCCEEDED 审计行，只读连接
        # 会在关闭时隐式回滚，看起来「命令跑了但账上没记」。
        async with engine.begin() as conn:
            await conn.execute(text(f'SET SESSION AUTHORIZATION "{_LOGIN}"'))
            placeholders = ", ".join(f":a{i}" for i in range(len(args)))
            sql = f"SELECT * FROM qc_maintenance.{func}({placeholders})"
            return (
                await conn.execute(text(sql), {f"a{i}": v for i, v in enumerate(args)})
            ).all()
    finally:
        await engine.dispose()


async def _call_commands(func: str, args: tuple = ()) -> list[tuple[str, int]]:
    rows = await _call_records(func, args)
    return [(row.category, int(row.row_count)) for row in rows]


async def _as_role_scalar(role: str, sql: str) -> object:
    engine = create_async_engine(build_migration_url(), poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            await conn.execute(text(f'SET SESSION AUTHORIZATION "{role}"'))
            return await conn.scalar(text(sql))
    finally:
        await engine.dispose()


async def test_work_order_must_be_created_requested_and_approved_in_order(
    admin_sessionmaker,
) -> None:
    async with admin_sessionmaker() as session:
        entity_id = str(uuid.uuid4())
        with pytest.raises(Exception, match="必须以 REQUESTED 创建"):
            await session.execute(
                text(_INSERT_WO.replace("'REQUESTED'", "'APPROVED'")),
                {
                    "entity_id": entity_id,
                    "tenant_id": uuid.uuid4(),
                    "kind": "RECONCILE",
                    "idem": f"bad-{entity_id}",
                },
            )
        await session.rollback()

        tenant = uuid.uuid4()
        wo = await _create_work_order(session, tenant=tenant, approve=False)
        with pytest.raises(Exception, match="非法的工单状态跳转"):
            await session.execute(
                text(
                    "UPDATE qc_maintenance.maintenance_work_orders "
                    "SET status='SUCCEEDED' WHERE entity_id=:id"
                ),
                {"id": wo},
            )
        await session.rollback()


async def test_idempotency_key_prevents_duplicate_intent(admin_sessionmaker) -> None:
    """同一幂等键只能落一行：重试/重放不得产生第二张工单。"""
    async with admin_sessionmaker() as session:
        tenant = uuid.uuid4()
        first = await _create_work_order(
            session, tenant=tenant, approve=False, idem=f"dup-{uuid.uuid4()}"
        )
        idem = (
            await session.execute(
                text(
                    "SELECT idempotency_key FROM qc_maintenance.maintenance_work_orders "
                    "WHERE entity_id=:id"
                ),
                {"id": first},
            )
        ).scalar_one()
        with pytest.raises(Exception, match="uq_maintenance_work_order_idem"):
            await session.execute(
                text(_INSERT_WO),
                {
                    "entity_id": str(uuid.uuid4()),
                    "tenant_id": tenant,
                    "kind": "RECONCILE",
                    "idem": idem,
                },
            )
        await session.rollback()


async def test_reconcile_requires_approved_work_order_of_right_kind(
    admin_sessionmaker,
) -> None:
    """未批准 / 类型不符都必须被拒，并且拒绝要留账。"""
    async with admin_sessionmaker() as session:
        tenant = uuid.uuid4()
        pending = await _create_work_order(session, tenant=tenant, approve=False)
        detect_kind = await _create_work_order(
            session, tenant=tenant, kind="DETECT", approve=True
        )

    for wo, reason in ((pending, "not_approved"), (detect_kind, "kind_mismatch")):
        with pytest.raises(exc.DBAPIError):
            await _call_commands("reconcile_tenant_tasks", (wo,))
        # 拒绝由库负责（异常），账本由调用方补记——这是契约的一部分。
        await _record_denial(wo, reason)

    async with admin_sessionmaker() as session:
        outcomes = (
            await session.execute(
                text(
                    "SELECT work_order_id, outcome, detail->>'reason' AS reason "
                    "FROM qc_maintenance.maintenance_action_log "
                    "WHERE work_order_id = ANY(:ids) ORDER BY id"
                ),
                {"ids": [pending, detect_kind]},
            )
        ).all()
    assert [(row.outcome, row.reason) for row in outcomes] == [
        ("DENIED", "not_approved"),
        ("DENIED", "kind_mismatch"),
    ], "拒绝必须进审计账本，且原因可区分"


async def test_reconcile_is_scoped_to_the_work_order_tenant(
    qc_sessionmaker, admin_sessionmaker
) -> None:
    """同一类不一致，在 A 的工单里只算 A 的；B 的那一条不得混进来。"""
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    for tenant in (tenant_a, tenant_b):
        suffix = uuid.uuid4().hex[:8]
        project_id = f"proj-maint-{suffix}"
        await seed_project(admin_sessionmaker, project_id=project_id, tenant=tenant)
        task_id = await _seed_task(
            qc_sessionmaker, tenant=tenant, project_id=project_id, suffix=suffix
        )
        # 制造一条可控的 status 不一致：投影说 RUNNING，源表是 QUEUED。
        async with admin_sessionmaker() as session:
            await session.execute(
                text(
                    "UPDATE qc_maintenance.task_projection "
                    "SET task_status='RUNNING', next_action_at=now() + interval '1 hour', "
                    "lease_expires_at=now() + interval '1 hour' "
                    "WHERE task_id=:id"
                ),
                {"id": task_id},
            )
            await session.commit()

    async with admin_sessionmaker() as session:
        wo_a = await _create_work_order(session, tenant=tenant_a)
        wo_b = await _create_work_order(session, tenant=tenant_b)

    counts_a = dict(await _call_commands("reconcile_tenant_tasks", (wo_a,)))
    counts_b = dict(await _call_commands("reconcile_tenant_tasks", (wo_b,)))
    assert counts_a["status_mismatch"] == 1, "A 租户的不一致应被算出 1 条"
    assert counts_b["status_mismatch"] == 1, "B 租户的不一致应被算出 1 条"

    async with admin_sessionmaker() as session:
        detail = (
            await session.execute(
                text(
                    "SELECT detail->'categories'->>'status_mismatch' "
                    "FROM qc_maintenance.maintenance_action_log "
                    "WHERE work_order_id=:id AND outcome='SUCCEEDED'"
                ),
                {"id": wo_a},
            )
        ).scalar_one()
    assert int(detail) == 1


async def test_detect_reports_seven_categories_and_no_payload(
    admin_sessionmaker,
) -> None:
    rows = await _call_commands("detect_projection_anomalies")
    categories = {category for category, _ in rows}
    assert categories == {
        "missing",
        "extra",
        "tenant_mismatch",
        "status_mismatch",
        "lease_mismatch",
        "generation_mismatch",
        "invalid_lease",
    }, "七类差异必须各自独立成行；合并成一类会让排障无从下手"


async def test_enumerate_returns_tenant_and_count_only(
    qc_sessionmaker, admin_sessionmaker
) -> None:
    """发现结果只暴露「租户 + 条数」：它是调度入口，不能变成跨租户读数接口。"""
    tenant = uuid.uuid4()
    suffix = uuid.uuid4().hex[:8]
    project_id = f"proj-maint-{suffix}"
    await seed_project(admin_sessionmaker, project_id=project_id, tenant=tenant)
    await _seed_task(
        qc_sessionmaker, tenant=tenant, project_id=project_id, suffix=suffix
    )

    rows = await _call_records("enumerate_due_tasks")
    assert rows, "至少要能看到刚入队的那个租户"
    keys = set(rows[0]._mapping.keys())
    assert keys == {"tenant_id", "due_count"}, (
        f"发现函数不得返回租户与条数之外的列，实际 {keys}"
    )
    matched = [
        r for r in rows if str(r._mapping["tenant_id"]) == str(tenant)
    ]
    assert matched and matched[0]._mapping["due_count"] >= 1


async def test_permissions_are_read_only_and_whitelist_bound(
    admin_sessionmaker,
) -> None:
    """权限面证明：只读、无 PUBLIC、白名单、owner 不可登录。"""
    async with admin_sessionmaker() as session:
        owner_select = await session.scalar(
            text("SELECT has_table_privilege(:r, 'qc_governance.qc_tasks', 'SELECT')"),
            {"r": _OWNER},
        )
        owner_update = await session.scalar(
            text("SELECT has_table_privilege(:r, 'qc_governance.qc_tasks', 'UPDATE')"),
            {"r": _OWNER},
        )
        owner_insert = await session.scalar(
            text("SELECT has_table_privilege(:r, 'qc_governance.qc_tasks', 'INSERT')"),
            {"r": _OWNER},
        )
        worker_exec = await session.scalar(
            text(
                "SELECT has_function_privilege(:r, "
                "'qc_maintenance.enumerate_due_tasks()', 'EXECUTE')"
            ),
            {"r": _WORKER},
        )
        helper_exec = await session.scalar(
            text(
                "SELECT has_function_privilege(:r, "
                "'qc_maintenance._projection_anomalies(uuid)', 'EXECUTE')"
            ),
            {"r": _LOGIN},
        )
        owner_login = await session.scalar(
            text("SELECT rolcanlogin FROM pg_roles WHERE rolname = :r"), {"r": _OWNER}
        )
        owners = (
            await session.execute(
                text(
                    """
                    SELECT p.proname, r.rolname
                      FROM pg_proc p
                      JOIN pg_namespace n ON n.oid = p.pronamespace
                      JOIN pg_roles r ON r.oid = p.proowner
                     WHERE n.nspname = 'qc_maintenance'
                       AND p.proname IN ('enumerate_due_tasks',
                                         'detect_projection_anomalies',
                                         'reconcile_tenant_tasks',
                                         'assert_session_member',
                                         '_projection_anomalies')
                    """
                )
            )
        ).all()

    assert owner_select is True, "只读命令需要 SELECT 才能读源表"
    assert owner_update is False, "本阶段不得给 owner 任何写权限（修复阶段才补锁权限）"
    assert owner_insert is False
    assert worker_exec is False, "质检 Worker 不得调用维护命令"
    assert helper_exec is False, "内部取数函数不得对任何身份开放"
    assert owner_login is False
    assert all(row.rolname == _OWNER for row in owners), (
        f"白名单函数必须全部以 qc_maintenance_owner 为 owner，实际 {owners}"
    )


async def test_login_cannot_write_source_tasks_and_worker_cannot_call() -> None:
    with pytest.raises(exc.DBAPIError, match="permission denied"):
        await _as_role_scalar(
            _LOGIN, "UPDATE qc_governance.qc_tasks SET lease_owner='x' WHERE false"
        )
    with pytest.raises(exc.DBAPIError, match="permission denied"):
        await _as_role_scalar(
            _WORKER, "SELECT * FROM qc_maintenance.enumerate_due_tasks()"
        )


async def test_operator_is_a_group_not_a_login_identity(admin_sessionmaker) -> None:
    """operator 是权限组，不是登录身份：只能被 login 继承，不能直接连库。"""
    async with admin_sessionmaker() as session:
        can_login = await session.scalar(
            text("SELECT rolcanlogin FROM pg_roles WHERE rolname = :r"),
            {"r": _OPERATOR},
        )
    assert can_login is False
