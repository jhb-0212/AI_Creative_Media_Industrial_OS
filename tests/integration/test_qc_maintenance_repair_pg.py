"""修复命令（WP-A.2）的真库验收：唯一会写数据的维护命令，必须写得住、也拦得住。

这一层的风险不在「函数能不能跑」，而在四条边界：授权、租户、锁、原子性。

1. **授权**：没有 ``REPAIR`` 工单、工单未批准、目标不是这张任务，一律拒绝；
   拒绝由库抛出，账由适配层补记，两者不能互相替代。
2. **租户**：工单租户与任务租户不一致时不得修——否则一张工单能跨租户写投影。
3. **锁**：固定锁序 ``qc_tasks`` → ``task_projection``，锁在**读源行之前**取；
   拿不到锁时在 ``lock_timeout`` 内失败，且**不留半成品**。
4. **原子与幂等**：投影与源行逐字段一致；同 ``attempt_id`` 重试只记一次、不再动数据。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import exc, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from packages.shared.config import get_settings  # noqa: F401
from packages.shared.database import (
    build_migration_url,
    create_qc_governance_engine,
    create_qc_maintenance_engine,
    create_sessionmaker,
)
from packages.shared.db_roles import domain_role, maintenance_role
from packages.shared.outbox_writer import SqlAlchemyOutboxWriter
from services.qc_governance.domain.qc_task import EnqueueOrigin
from services.qc_governance.maintenance.command_client import MaintenanceCommandClient
from services.qc_governance.repository.qc_task_repo import SqlAlchemyQCTaskRepository
from services.qc_governance.repository.task_projection_repo import (
    SqlAlchemyTaskProjectionRepository,
)
from services.qc_governance.service.qc_task_service import QCTaskService
from tests.conftest import set_test_scope
from tests.integration.maintenance_seed import create_work_order
from tests.integration.planning_seed import seed_project

_LOGIN = maintenance_role("login")
# Worker 的运行身份（域角色）；qc_worker 壳身份已退役（迁移 f2b3c4d5e6a7）。
_WORKER = domain_role("qc_governance")
_OWNER = maintenance_role("owner")
_PUBLIC = "PUBLIC"

_MAINTAIN_SINCE = 170000


@pytest.fixture(scope="module")
async def qc_sessionmaker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_qc_governance_engine()
    sm = create_sessionmaker(engine)
    try:
        yield sm
    finally:
        await engine.dispose()


@pytest.fixture()
async def client() -> AsyncIterator[MaintenanceCommandClient]:
    engine = create_qc_maintenance_engine()
    try:
        yield MaintenanceCommandClient(engine)
    finally:
        await engine.dispose()


async def _seed_task(
    sm: async_sessionmaker[AsyncSession],
    admin_sm: async_sessionmaker[AsyncSession],
    *,
    tenant,
    suffix: str,
) -> tuple[str, str]:
    """入队一个 QC 任务，返回 ``(task_id, project_id)``。"""
    project_id = f"proj-repair-{suffix}"
    await seed_project(admin_sm, project_id=project_id, tenant=tenant)
    async with sm() as session:
        await set_test_scope(session, tenant_id=tenant)
        service = QCTaskService(
            SqlAlchemyQCTaskRepository(session),
            SqlAlchemyOutboxWriter(session),
            projection=SqlAlchemyTaskProjectionRepository(session),
        )
        task = await service.enqueue(
            project_id=project_id,
            asset_version_id=f"av-repair-{suffix}",
            generation_task_id=f"gen-repair-{suffix}",
            quality_profile_version=f"qp-{suffix}",
            tenant_id=tenant,
            origin=EnqueueOrigin(source="RELAY", operator_id="outbox-relay"),
            shot_id=f"shot-{suffix}",
            qc_task_id=f"qc-repair-{suffix}",
        )
        await session.commit()
        return task.entity_id, project_id


def _login_engine() -> AsyncEngine:
    return create_async_engine(build_migration_url(), poolclass=NullPool)


async def _call_repair(work_order_id: str, task_id: str, attempt_id: str) -> list:
    """以维护登录身份调用修复命令；必须 ``begin()``，否则审计行被隐式回滚。"""
    engine = _login_engine()
    try:
        async with engine.begin() as conn:
            await conn.execute(text(f'SET SESSION AUTHORIZATION "{_LOGIN}"'))
            return (
                await conn.execute(
                    text(
                        "SELECT * FROM qc_maintenance.repair_tenant_task"
                        "(:wo, :task, :attempt)"
                    ),
                    {"wo": work_order_id, "task": task_id, "attempt": attempt_id},
                )
            ).all()
    finally:
        await engine.dispose()


async def _record_denial(work_order_id: str, reason: str, attempt_id: str) -> None:
    """以维护登录身份补记一行失败账（证明原因在白名单内、且 action 可区分）。"""
    engine = _login_engine()
    try:
        async with engine.begin() as conn:
            await conn.execute(text(f'SET SESSION AUTHORIZATION "{_LOGIN}"'))
            await conn.execute(
                text(
                    "SELECT qc_maintenance.record_maintenance_denial"
                    "(:wo, :reason, :attempt, 'DENIED', 'repair_tenant_task')"
                ),
                {"wo": work_order_id, "reason": reason, "attempt": attempt_id},
            )
    finally:
        await engine.dispose()


async def _projection(admin_sm, task_id: str) -> dict | None:
    async with admin_sm() as session:
        row = (
            await session.execute(
                text(
                    "SELECT task_id, tenant_id, project_id, task_status, next_action_at, "
                    "lease_expires_at, lease_generation, entity_version "
                    "FROM qc_maintenance.task_projection WHERE task_id = :id"
                ),
                {"id": task_id},
            )
        ).first()
    return dict(row._mapping) if row is not None else None


async def _source(admin_sm, task_id: str) -> dict:
    async with admin_sm() as session:
        row = (
            await session.execute(
                text(
                    "SELECT entity_id, tenant_id, project_id, status, created_at, "
                    "lease_expires_at, lease_generation, entity_version "
                    "FROM qc_governance.qc_tasks WHERE entity_id = :id"
                ),
                {"id": task_id},
            )
        ).one()
    return dict(row._mapping)


async def _corrupt_projection(admin_sm, task_id: str) -> None:
    """把投影改成一条不可能来自源行的记录，用来证明修复真的重写了它。"""
    async with admin_sm() as session:
        await session.execute(
            text(
                "UPDATE qc_maintenance.task_projection "
                "SET task_status = 'RUNNING', "
                "    next_action_at = now() + interval '7 days', "
                "    lease_generation = 999 "
                "WHERE task_id = :id"
            ),
            {"id": task_id},
        )
        await session.commit()


async def _audit_rows(admin_sm, work_order_id: str) -> list:
    async with admin_sm() as session:
        return (
            await session.execute(
                text(
                    "SELECT attempt_id, action, outcome, detail->>'action' AS detail_action, "
                    "       detail->>'task_id' AS task_id, "
                    "       detail->>'rows_affected' AS rows_affected, "
                    "       detail->>'actor' AS actor, detail->>'reason' AS reason "
                    "FROM qc_maintenance.maintenance_action_log "
                    "WHERE work_order_id = :id ORDER BY id"
                ),
                {"id": work_order_id},
            )
        ).all()


async def test_repair_rebuilds_projection_to_match_source(
    qc_sessionmaker, admin_sessionmaker
) -> None:
    """投影被写坏后，修复要把每个字段还原成源行的唯一口径。"""
    tenant = uuid.uuid4()
    task_id, _ = await _seed_task(
        qc_sessionmaker, admin_sessionmaker, tenant=tenant, suffix=uuid.uuid4().hex[:8]
    )
    async with admin_sessionmaker() as session:
        wo = await create_work_order(
            session, tenant=tenant, kind="REPAIR", target_task_id=task_id
        )
    await _corrupt_projection(admin_sessionmaker, task_id)

    rows = await _call_repair(wo, task_id, f"repair-{uuid.uuid4().hex}")
    assert [(r.outcome, int(r.rows_affected)) for r in rows] == [("repaired", 1)]

    projection = await _projection(admin_sessionmaker, task_id)
    source = await _source(admin_sessionmaker, task_id)
    assert projection is not None
    assert projection["task_status"] == source["status"] == "QUEUED"
    assert projection["lease_generation"] == source["lease_generation"] == 0
    assert projection["entity_version"] == source["entity_version"]
    assert projection["tenant_id"] == source["tenant_id"] == tenant
    assert projection["project_id"] == source["project_id"]
    assert projection["next_action_at"] == source["created_at"], (
        "QUEUED 的 next_action_at 必须等于 created_at：投影不能引入第二个时间源"
    )

    audit = await _audit_rows(admin_sessionmaker, wo)
    assert len(audit) == 1
    assert (audit[0].action, audit[0].outcome) == ("repair_tenant_task", "SUCCEEDED")
    assert audit[0].detail_action == "upsert"
    assert audit[0].task_id == task_id
    assert audit[0].rows_affected == "1"
    assert audit[0].actor == _LOGIN, "审计主体必须来自 session_user"


async def test_repair_prunes_projection_whose_source_row_is_gone(
    admin_sessionmaker,
) -> None:
    """源行不存在、投影还留着 → 剪除。幽灵候选不会被调度永远重试。"""
    tenant = uuid.uuid4()
    project_id = f"proj-ghost-{uuid.uuid4().hex[:8]}"
    await seed_project(
        admin_sessionmaker, project_id=project_id, tenant=tenant
    )
    ghost = str(uuid.uuid4())
    async with admin_sessionmaker() as session:
        await session.execute(
            text(
                "INSERT INTO qc_maintenance.task_projection "
                "(task_id, tenant_id, project_id, task_status, next_action_at, "
                " lease_generation, entity_version, updated_at) "
                "VALUES (:id, :tenant, :project, 'QUEUED', now(), 0, 1, now())"
            ),
            {"id": ghost, "tenant": tenant, "project": project_id},
        )
        await session.commit()
        wo = await create_work_order(
            session, tenant=tenant, kind="REPAIR", target_task_id=ghost
        )

    rows = await _call_repair(wo, ghost, f"prune-{uuid.uuid4().hex}")
    assert [(r.outcome, int(r.rows_affected)) for r in rows] == [("pruned", 1)]
    assert await _projection(admin_sessionmaker, ghost) is None
    audit = await _audit_rows(admin_sessionmaker, wo)
    assert audit[0].detail_action == "prune"


async def test_repair_retires_projection_for_terminal_task(
    qc_sessionmaker, admin_sessionmaker
) -> None:
    """任务已经 PASSED/REJECTED：投影必须被撤掉，而不是被保留成待办。"""
    tenant = uuid.uuid4()
    task_id, _ = await _seed_task(
        qc_sessionmaker, admin_sessionmaker, tenant=tenant, suffix=uuid.uuid4().hex[:8]
    )
    async with admin_sessionmaker() as session:
        await session.execute(
            text(
                "UPDATE qc_governance.qc_tasks SET status = 'PASSED' "
                "WHERE entity_id = :id"
            ),
            {"id": task_id},
        )
        await session.commit()
        wo = await create_work_order(
            session, tenant=tenant, kind="REPAIR", target_task_id=task_id
        )
    await _corrupt_projection(admin_sessionmaker, task_id)

    rows = await _call_repair(wo, task_id, f"retire-{uuid.uuid4().hex}")
    assert [(r.outcome, int(r.rows_affected)) for r in rows] == [("retired", 1)]
    assert await _projection(admin_sessionmaker, task_id) is None


async def test_repair_refuses_unauthorized_calls(
    qc_sessionmaker, admin_sessionmaker
) -> None:
    """四类授权失败必须被库拒绝，且原因可区分、可补记。"""
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    task_a, _ = await _seed_task(
        qc_sessionmaker, admin_sessionmaker, tenant=tenant_a, suffix=uuid.uuid4().hex[:8]
    )
    task_b, _ = await _seed_task(
        qc_sessionmaker, admin_sessionmaker, tenant=tenant_b, suffix=uuid.uuid4().hex[:8]
    )
    async with admin_sessionmaker() as session:
        wrong_kind = await create_work_order(
            session, tenant=tenant_a, kind="RECONCILE"
        )
        pending = await create_work_order(
            session,
            tenant=tenant_a,
            kind="REPAIR",
            target_task_id=task_a,
            approve=False,
        )
        other_target = await create_work_order(
            session, tenant=tenant_a, kind="REPAIR", target_task_id=task_b
        )
        cross_tenant = await create_work_order(
            session, tenant=tenant_b, kind="REPAIR", target_task_id=task_a
        )
    missing = f"no-such-{uuid.uuid4().hex}"

    cases = (
        (wrong_kind, task_a, "kind_mismatch"),
        (pending, task_a, "not_approved"),
        (other_target, task_a, "task_not_authorized"),
        (cross_tenant, task_a, "tenant_mismatch"),
        (missing, task_a, "work_order_not_found"),
    )
    for wo, task_id, reason in cases:
        with pytest.raises(exc.DBAPIError):
            await _call_repair(wo, task_id, f"deny-{uuid.uuid4().hex}")
        if wo == missing:
            # 工单不存在时账本无处可挂：这是一个已知的审计缺口，不能假装记过。
            continue
        await _record_denial(wo, reason, f"deny-{uuid.uuid4().hex}")
        audit = await _audit_rows(admin_sessionmaker, wo)
        assert audit and audit[-1].reason == reason, (
            f"拒绝原因 {reason} 必须能落账，否则越权尝试在审计里不存在"
        )
        assert audit[-1].action == "repair_tenant_task", (
            "账本 action 必须与实际命令一致，不能一律记成 reconcile"
        )


async def test_repair_is_idempotent_per_attempt(
    qc_sessionmaker, admin_sessionmaker
) -> None:
    """同一 attempt 重放返回 already_applied 且不再写数据；换 attempt 才会重做。"""
    tenant = uuid.uuid4()
    task_id, _ = await _seed_task(
        qc_sessionmaker, admin_sessionmaker, tenant=tenant, suffix=uuid.uuid4().hex[:8]
    )
    async with admin_sessionmaker() as session:
        wo = await create_work_order(
            session, tenant=tenant, kind="REPAIR", target_task_id=task_id
        )
    await _corrupt_projection(admin_sessionmaker, task_id)

    attempt = f"idem-{uuid.uuid4().hex}"
    first = await _call_repair(wo, task_id, attempt)
    assert [(r.outcome, int(r.rows_affected)) for r in first] == [("repaired", 1)]
    second = await _call_repair(wo, task_id, attempt)
    assert [(r.outcome, int(r.rows_affected)) for r in second] == [
        ("already_applied", 0)
    ]

    audit = await _audit_rows(admin_sessionmaker, wo)
    assert len(audit) == 1, "同一次尝试不得记两行"

    # 新的尝试：数据已经正确，重做仍然是幂等的 upsert（不会重复插行）。
    third = await _call_repair(wo, task_id, f"again-{uuid.uuid4().hex}")
    assert [(r.outcome, int(r.rows_affected)) for r in third] == [("repaired", 1)]
    assert len(await _audit_rows(admin_sessionmaker, wo)) == 2


async def test_repair_lock_timeout_leaves_projection_untouched(
    qc_sessionmaker, admin_sessionmaker
) -> None:
    """另一个会话握着 ``SHARE ROW EXCLUSIVE`` 时，修复必须在 5s 内失败且不落半成品。"""
    tenant = uuid.uuid4()
    task_id, _ = await _seed_task(
        qc_sessionmaker, admin_sessionmaker, tenant=tenant, suffix=uuid.uuid4().hex[:8]
    )
    async with admin_sessionmaker() as session:
        wo = await create_work_order(
            session, tenant=tenant, kind="REPAIR", target_task_id=task_id
        )
    await _corrupt_projection(admin_sessionmaker, task_id)
    before = await _projection(admin_sessionmaker, task_id)

    blocker = create_async_engine(build_migration_url(), poolclass=NullPool)
    repair_engine = _login_engine()
    try:
        async with blocker.connect() as hold:
            await hold.execute(
                text(
                    "LOCK TABLE qc_governance.qc_tasks IN SHARE ROW EXCLUSIVE MODE"
                )
            )

            async def _attempt() -> None:
                async with repair_engine.begin() as conn:
                    await conn.execute(text(f'SET SESSION AUTHORIZATION "{_LOGIN}"'))
                    await conn.execute(
                        text(
                            "SELECT * FROM qc_maintenance.repair_tenant_task"
                            "(:wo, :task, :attempt)"
                        ),
                        {
                            "wo": wo,
                            "task": task_id,
                            "attempt": f"lock-{uuid.uuid4().hex}",
                        },
                    )

            with pytest.raises(exc.DBAPIError, match="lock"):
                await asyncio.wait_for(_attempt(), timeout=30)
            await hold.rollback()
    finally:
        await blocker.dispose()
        await repair_engine.dispose()

    assert await _projection(admin_sessionmaker, task_id) == before, (
        "拿不到锁就必须整体回滚：不允许改了一半的投影"
    )
    assert await _audit_rows(admin_sessionmaker, wo) == [], (
        "失败事务里的 SUCCEEDED 审计行不能残留"
    )


async def test_repair_privileges_match_server_version(admin_sessionmaker) -> None:
    """PG17+ 用 ``MAINTAIN``；PG16 用 ``UPDATE`` 兼容，且只此一项、不越界。

    ``UPDATE`` 在 PG16 上确实给了 owner 写源表的能力（版本限制，不是设计选择），
    所以这里同时钉住「只有 UPDATE」以及「login 仍无写权限」，把风险面量化。
    """
    async with admin_sessionmaker() as session:
        version = int(
            await session.scalar(text("SELECT current_setting('server_version_num')"))
        )

        async def _priv(role: str, privilege: str) -> bool:
            return bool(
                await session.scalar(
                    text(
                        "SELECT has_table_privilege(:r, 'qc_governance.qc_tasks', :p)"
                    ),
                    {"r": role, "p": privilege},
                )
            )

        owner_update = await _priv(_OWNER, "UPDATE")
        owner_insert = await _priv(_OWNER, "INSERT")
        owner_delete = await _priv(_OWNER, "DELETE")
        owner_truncate = await _priv(_OWNER, "TRUNCATE")
        login_update = await _priv(_LOGIN, "UPDATE")
        public_exec = await session.scalar(
            text(
                """
                SELECT count(*)
                  FROM pg_proc p
                  JOIN pg_namespace n ON n.oid = p.pronamespace
                  CROSS JOIN LATERAL aclexplode(p.proacl) a
                 WHERE n.nspname = 'qc_maintenance'
                   AND p.proname = 'repair_tenant_task'
                   AND a.grantee = 0
                   AND a.privilege_type = 'EXECUTE'
                """
            )
        )
        worker_exec = await session.scalar(
            text(
                "SELECT has_function_privilege(:r, "
                "'qc_maintenance.repair_tenant_task(text, text, text)', 'EXECUTE')"
            ),
            {"r": _WORKER},
        )
        login_exec = await session.scalar(
            text(
                "SELECT has_function_privilege(:r, "
                "'qc_maintenance.repair_tenant_task(text, text, text)', 'EXECUTE')"
            ),
            {"r": _LOGIN},
        )

    assert owner_insert is False
    assert owner_delete is False
    assert owner_truncate is False, "修复只需要「锁表」这一项，不得顺势拿到删表能力"
    assert login_update is False, "维护 login 不得绕过命令直接写源表"
    assert public_exec == 0, "命令不得对 PUBLIC 开放"
    assert worker_exec is False, "质检 Worker 不得调用修复"
    assert login_exec is True

    if version >= _MAINTAIN_SINCE:
        maintain = await _has_maintain(admin_sessionmaker, _OWNER)
        assert maintain is True, "PG17+ 必须走 MAINTAIN，而不是退回 UPDATE"
        assert owner_update is False, "升级到 MAINTAIN 后必须收回旧 UPDATE"
    else:
        assert owner_update is True, (
            f"PG{version} 没有 MAINTAIN，只能用 UPDATE 取锁；这是版本限制下的例外"
        )


async def _has_maintain(admin_sm, role: str) -> bool:
    async with admin_sm() as session:
        return bool(
            await session.scalar(
                text(
                    "SELECT has_table_privilege(:r, 'qc_governance.qc_tasks', 'MAINTAIN')"
                ),
                {"r": role},
            )
        )


async def test_client_repair_records_denial_with_repair_action(
    client, qc_sessionmaker, admin_sessionmaker
) -> None:
    """适配层走修复命令：被拒时补记的行必须标 repair_tenant_task，而不是 reconcile。"""
    tenant = uuid.uuid4()
    task_id, _ = await _seed_task(
        qc_sessionmaker, admin_sessionmaker, tenant=tenant, suffix=uuid.uuid4().hex[:8]
    )
    async with admin_sessionmaker() as session:
        pending = await create_work_order(
            session,
            tenant=tenant,
            kind="REPAIR",
            target_task_id=task_id,
            approve=False,
        )

    result = await client.repair_task(
        pending, task_id, attempt_id=f"cli-deny-{uuid.uuid4().hex}"
    )
    assert result.denial == "not_approved"
    assert result.denial_recorded is True
    assert result.rows == []

    audit = await _audit_rows(admin_sessionmaker, pending)
    assert [(row.action, row.outcome) for row in audit] == [
        ("repair_tenant_task", "DENIED")
    ]
    assert audit[0].reason == "not_approved"
    assert audit[0].actor == _LOGIN


async def test_client_repair_success_path(
    client, qc_sessionmaker, admin_sessionmaker
) -> None:
    tenant = uuid.uuid4()
    task_id, _ = await _seed_task(
        qc_sessionmaker, admin_sessionmaker, tenant=tenant, suffix=uuid.uuid4().hex[:8]
    )
    async with admin_sessionmaker() as session:
        wo = await create_work_order(
            session, tenant=tenant, kind="REPAIR", target_task_id=task_id
        )
    await _corrupt_projection(admin_sessionmaker, task_id)

    result = await client.repair_task(
        wo, task_id, attempt_id=f"cli-ok-{uuid.uuid4().hex}"
    )
    assert result.denial is None
    assert result.denial_recorded is True
    assert result.rows == [{"outcome": "repaired", "rows_affected": 1}]
    assert (await _projection(admin_sessionmaker, task_id))["task_status"] == "QUEUED"
