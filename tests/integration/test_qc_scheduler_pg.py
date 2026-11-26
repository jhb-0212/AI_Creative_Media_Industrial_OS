"""调度发现身份与租户委派的真库验收（WP-B V11 / V18 的最小闭环）。

证明三件事，缺一都不是「隔离」而只是「换了个名字」：

1. **发现身份只能发现**：``qc_scheduler`` 能调 ``enumerate_due_tasks()``，
   但对 ``qc_tasks`` 与 ``task_projection`` 没有任何表权限，维护命令也调不动。
2. **发现覆盖待检租户**：每个有待检任务的租户都被报出来，没有活的不报。
3. **接手仍在租户边界内**：按租户领取只会动该租户的任务，另一个租户的
   QUEUED 任务保持不动——「知道租户 id」不等于「能跨租户取活」。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

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
    create_qc_scheduler_engine,
    create_sessionmaker,
)
from packages.shared.db_roles import maintenance_role, scheduler_role
from packages.shared.outbox_writer import SqlAlchemyOutboxWriter
from services.qc_governance.domain.qc_task import EnqueueOrigin
from services.qc_governance.repository.qc_task_repo import SqlAlchemyQCTaskRepository
from services.qc_governance.repository.task_projection_repo import (
    SqlAlchemyTaskProjectionRepository,
)
from services.qc_governance.service.qc_task_service import QCTaskService
from tests.conftest import set_test_scope
from tests.integration.planning_seed import seed_project
from workers.qc.__main__ import _make_scope, _make_tenant_scope
from workers.qc.scheduler import MaintenanceTenantDiscovery
from workers.qc.worker import QCWorker

_SCHEDULER = scheduler_role()
_LOGIN = maintenance_role("login")
_OWNER = maintenance_role("owner")


@pytest.fixture(scope="module")
async def qc_sessionmaker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_qc_governance_engine()
    sm = create_sessionmaker(engine)
    try:
        yield sm
    finally:
        await engine.dispose()


class _NeverInspects:
    """领取后不产出结论的检测器：让任务停在 RUNNING，便于观察租户边界。"""

    configured = True

    async def inspect(self, task: Any) -> None:
        return None


async def _seed_tenant_task(
    qc_sm: async_sessionmaker[AsyncSession],
    admin_sm: async_sessionmaker[AsyncSession],
    *,
    suffix: str,
) -> tuple[uuid.UUID, str]:
    tenant = uuid.uuid4()
    project_id = f"proj-sched-{suffix}"
    await seed_project(admin_sm, project_id=project_id, tenant=tenant)
    async with qc_sm() as session:
        await set_test_scope(session, tenant_id=tenant)
        service = QCTaskService(
            SqlAlchemyQCTaskRepository(session),
            SqlAlchemyOutboxWriter(session),
            projection=SqlAlchemyTaskProjectionRepository(session),
        )
        task = await service.enqueue(
            project_id=project_id,
            asset_version_id=f"av-sched-{suffix}",
            generation_task_id=f"gen-sched-{suffix}",
            quality_profile_version=f"qp-{suffix}",
            tenant_id=tenant,
            origin=EnqueueOrigin(source="RELAY", operator_id="outbox-relay"),
            shot_id=f"shot-{suffix}",
            qc_task_id=f"qc-sched-{suffix}",
        )
        await session.commit()
        return tenant, task.entity_id


def _as_role_engine(role: str) -> AsyncEngine:
    return create_async_engine(build_migration_url(), poolclass=NullPool)


async def test_scheduler_identity_can_only_discover(admin_sessionmaker) -> None:
    """授权面：发现函数能调，表读与维护命令都不能。"""
    async with admin_sessionmaker() as session:
        can_discover = await session.scalar(
            text(
                "SELECT has_function_privilege(:r, "
                "'qc_maintenance.enumerate_due_tasks()', 'EXECUTE')"
            ),
            {"r": _SCHEDULER},
        )
        for function in (
            "qc_maintenance.reconcile_tenant_tasks(text)",
            "qc_maintenance.repair_tenant_task(text, text, text)",
            "qc_maintenance.detect_projection_anomalies()",
        ):
            allowed = await session.scalar(
                text("SELECT has_function_privilege(:r, :f, 'EXECUTE')"),
                {"r": _SCHEDULER, "f": function},
            )
            assert allowed is False, f"Scheduler 不得调用 {function}"
        # 白名单不是「我们点名的这几个都不行」，而是「schema 里除发现函数外
        # 一个都不行」。这一句专治漏网：函数新建时默认带 PUBLIC EXECUTE，
        # 少收一个，调度身份就经 PUBLIC 拿到它（触发器函数改 migrations 时
        # 真出现过这种残留）。
        leaked = await session.execute(
            text(
                "SELECT p.proname FROM pg_proc p "
                "JOIN pg_namespace n ON n.oid = p.pronamespace "
                "WHERE n.nspname = 'qc_maintenance' "
                "AND p.proname <> 'enumerate_due_tasks' "
                "AND has_function_privilege(:r, p.oid, 'EXECUTE')"
            ),
            {"r": _SCHEDULER},
        )
        assert leaked.scalars().all() == [], (
            f"调度身份不该能执行发现函数以外的维护函数，实际 {leaked.scalars().all()}"
        )
        for table in ("qc_governance.qc_tasks", "qc_maintenance.task_projection"):
            allowed = await session.scalar(
                text("SELECT has_table_privilege(:r, :t, 'SELECT')"),
                {"r": _SCHEDULER, "t": table},
            )
            assert allowed is False, f"Scheduler 不得直读 {table}"
        can_login = await session.scalar(
            text("SELECT rolcanlogin FROM pg_roles WHERE rolname = :r"),
            {"r": _SCHEDULER},
        )
        bypass = await session.scalar(
            text("SELECT rolbypassrls FROM pg_roles WHERE rolname = :r"),
            {"r": _SCHEDULER},
        )

    assert can_discover is True, "发现身份至少要能发现，否则无从调度"
    assert can_login is True
    assert bypass is False

    # 真调用一次：不仅 ACL 说不能读，实际读也必须被拒。必须用**发现身份自己的
    # DSN**——拿超级用户连接再 ``SET SESSION AUTHORIZATION`` 只是模拟，而模拟
    # 出的边界恰好证明不了「真实凭据降权」（例如角色继承、默认权限都看不见）。
    engine = create_qc_scheduler_engine()
    try:
        async with engine.connect() as conn:
            rows = (await conn.execute(text(
                "SELECT tenant_id, due_count "
                "FROM qc_maintenance.enumerate_due_tasks()"
            ))).all()
            assert rows, "发现函数应可执行并返回结果"
            keys = set(rows[0]._mapping.keys())
            assert keys == {"tenant_id", "due_count"}, (
                f"发现结果只暴露租户与条数，实际 {keys}"
            )
            with pytest.raises(exc.DBAPIError, match="permission denied"):
                await conn.execute(text("SELECT count(*) FROM qc_governance.qc_tasks"))
    finally:
        await engine.dispose()


async def test_discovery_reports_only_tenants_with_due_work(
    qc_sessionmaker, admin_sessionmaker
) -> None:
    tenant_a, _ = await _seed_tenant_task(
        qc_sessionmaker, admin_sessionmaker, suffix=uuid.uuid4().hex[:8]
    )
    tenant_quiet = uuid.uuid4()
    await seed_project(
        admin_sessionmaker, project_id=f"proj-quiet-{uuid.uuid4().hex[:8]}", tenant=tenant_quiet
    )

    engine = create_qc_scheduler_engine()
    try:
        tenants = await MaintenanceTenantDiscovery(engine).tenants()
    finally:
        await engine.dispose()

    assert tenants is not None, "发现失败不得被当成空队列"
    assert tenant_a in tenants
    assert tenant_quiet not in tenants, "没有待检任务的租户不应进入调度路径"


async def test_tenant_scoped_claim_stays_inside_its_tenant(
    qc_sessionmaker, admin_sessionmaker
) -> None:
    """按租户 A 领取不得动租户 B 的任务：两者都由 RLS 判定，而不是靠调用方自觉。"""
    tenant_a, task_a = await _seed_tenant_task(
        qc_sessionmaker, admin_sessionmaker, suffix=uuid.uuid4().hex[:8]
    )
    _tenant_b, task_b = await _seed_tenant_task(
        qc_sessionmaker, admin_sessionmaker, suffix=uuid.uuid4().hex[:8]
    )

    engine = create_qc_governance_engine()
    try:
        sm = create_sessionmaker(engine)
        worker = QCWorker(
            scope=_make_scope(sm),
            scope_for_tenant=_make_tenant_scope(sm),
            executor=_NeverInspects(),
            worker_id=f"sched-test-{uuid.uuid4().hex[:8]}",
            lease_seconds=120,
        )
        assert await worker.process_one(tenant=tenant_a) is True
    finally:
        await engine.dispose()

    async with admin_sessionmaker() as session:
        statuses = dict(
            (
                await session.execute(
                    text(
                        "SELECT entity_id, status FROM qc_governance.qc_tasks "
                        "WHERE entity_id = ANY(:ids)"
                    ),
                    {"ids": [task_a, task_b]},
                )
            ).all()
        )
        owners = dict(
            (
                await session.execute(
                    text(
                        "SELECT entity_id, lease_owner FROM qc_governance.qc_tasks "
                        "WHERE entity_id = ANY(:ids)"
                    ),
                    {"ids": [task_a, task_b]},
                )
            ).all()
        )

    assert statuses[task_a] == "RUNNING"
    assert statuses[task_b] == "QUEUED", "另一个租户的任务不得被这次领取影响"
    assert owners[task_b] in (None, ""), "未被领取的任务不该有租约持有者"
