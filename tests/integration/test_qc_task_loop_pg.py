"""WP-B 最小闭环的端到端验收：发现 → 租户内领取 → 执行 → 终结 / 回收。

与相邻用例的分工
----------------
* ``test_qc_scheduler_pg``：证明**发现身份**的授权面（能发现、不能读表）；
* 本用例：证明**闭环本身**——真实发现函数给出租户，真实 QC 池在该租户上下文里
  领取，执行后写入终态并撤掉投影；以及「领取后进程不再续租」时，清扫能按发现
  结果把过期租约回收成 QUEUED。

两处刻意的测试替身（其余都是真实组件）
--------------------------------------
1. **检测器**替身：这里验证的是循环装配，不是画质判定；用固定结论的检测器，
   避免把「检测器读不到产物」与「循环断了」混成同一个红灯。
2. **报告核验器**替身：报告与任务的一致性由 ``test_qc_task_projection_pg``
   等用例覆盖；这里只需要一个能证明「判定带着可核验的证据」的最小实现。

发现结果**不整体消费**：开发库里积着大量历史 QUEUED 任务，若让 run_once 处理
全部租户，用例会去改别人的数据。真实发现函数照常执行，只把返回结果过滤到本
用例的租户上——发现能力是真的，作用范围是本用例自己划的。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.shared.database import (
    create_qc_governance_engine,
    create_qc_scheduler_engine,
    create_sessionmaker,
)
from tests.integration.qc_seed import seed_tenant_task
from workers.qc.__main__ import _make_scope, _make_tenant_scope
from workers.qc.scheduler import MaintenanceTenantDiscovery, TenantScheduler
from workers.qc.worker import QCInspection, QCWorker


@pytest.fixture(scope="module")
async def qc_sessionmaker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """qc_governance_role 连接（与生产 Worker 同角色），只用于播种。"""
    engine = create_qc_governance_engine()
    try:
        yield create_sessionmaker(engine)
    finally:
        await engine.dispose()


class _ScopedDiscovery:
    """真实发现函数 + 用例侧过滤：只把本用例的租户交给调度。"""

    def __init__(self, *, engine: Any, tenants: set[uuid.UUID]) -> None:
        self._inner = MaintenanceTenantDiscovery(engine)
        self._tenants = tenants

    async def tenants(self) -> list[uuid.UUID] | None:
        found = await self._inner.tenants()
        if found is None:
            return None
        return [t for t in found if t in self._tenants]


class _PassingInspector:
    """固定 PASSED 的检测器；同时登记它「出具」的报告与任务事实。"""

    configured = True

    def __init__(self) -> None:
        self.reports: dict[str, tuple[str, uuid.UUID]] = {}

    async def inspect(self, task: Any) -> QCInspection:
        report_id = f"rep-loop-{uuid.uuid4().hex[:8]}"
        self.reports[report_id] = (task.asset_version_id, task.tenant_id)
        return QCInspection(result="PASSED", qc_report_id=report_id)


class _NeverInspects:
    """领取后不产出结论：让任务停在 RUNNING，等租约过期。"""

    configured = True

    async def inspect(self, task: Any) -> None:
        return None


def _report_lookup(reports: dict[str, tuple[str, uuid.UUID]]) -> Any:
    async def lookup(report_id: str, tenant_id: Any, organization_id: Any) -> Any:
        facts = reports.get(report_id)
        if facts is None:
            return None
        return SimpleNamespace(asset_version_id=facts[0], tenant_id=facts[1])

    return lookup


async def _status(admin_sm: async_sessionmaker[AsyncSession], task_id: str) -> Any:
    async with admin_sm() as session:
        return (
            await session.execute(
                text(
                    "SELECT status, lease_owner, lease_expires_at "
                    "FROM qc_governance.qc_tasks WHERE entity_id = :id"
                ),
                {"id": task_id},
            )
        ).one()


async def _projection_tenants(
    admin_sm: async_sessionmaker[AsyncSession], task_id: str
) -> int:
    async with admin_sm() as session:
        return int(
            await session.scalar(
                text(
                    "SELECT count(*) FROM qc_maintenance.task_projection "
                    "WHERE task_id = :id"
                ),
                {"id": task_id},
            )
        )


async def test_discovery_drives_claim_inspect_and_terminal_state(
    qc_sessionmaker,
    admin_sessionmaker,
) -> None:
    tenant_a, task_a = await seed_tenant_task(qc_sessionmaker, admin_sessionmaker)
    tenant_b, task_b = await seed_tenant_task(qc_sessionmaker, admin_sessionmaker)

    inspector = _PassingInspector()
    qc_engine = create_qc_governance_engine()
    scheduler_engine = create_qc_scheduler_engine()
    try:
        sm = create_sessionmaker(qc_engine)
        report_lookup = _report_lookup(inspector.reports)
        worker = QCWorker(
            scope=_make_scope(sm, report_lookup),
            scope_for_tenant=_make_tenant_scope(sm, report_lookup),
            executor=inspector,
            worker_id=f"loop-{uuid.uuid4().hex[:8]}",
            lease_seconds=120,
        )
        scheduler = TenantScheduler(
            discovery=_ScopedDiscovery(
                engine=scheduler_engine, tenants={tenant_a, tenant_b}
            ),
            worker=worker,
        )
        outcome = await scheduler.run_once()
    finally:
        await scheduler_engine.dispose()
        await qc_engine.dispose()

    assert outcome.blocked is False
    assert outcome.tenant_errors == 0
    assert outcome.busy is True

    row_a = await _status(admin_sessionmaker, task_a)
    assert row_a.status == "PASSED", "执行完成必须落终态，而不是停在 RUNNING"
    assert not row_a.lease_owner, "终结时租约必须撤销"

    row_b = await _status(admin_sessionmaker, task_b)
    assert row_b.status == "PASSED", "第二个被发现的租户同样要被处理（逐个租户委派）"

    assert await _projection_tenants(admin_sessionmaker, task_a) == 0, (
        "终态任务必须离开调度投影：留着它就等于让调度永远看到「有待检」"
    )
    assert await _projection_tenants(admin_sessionmaker, task_b) == 0


async def test_expired_lease_is_reclaimed_through_discovery(
    qc_sessionmaker,
    admin_sessionmaker,
) -> None:
    tenant_c, task_c = await seed_tenant_task(qc_sessionmaker, admin_sessionmaker)

    qc_engine = create_qc_governance_engine()
    scheduler_engine = create_qc_scheduler_engine()
    try:
        sm = create_sessionmaker(qc_engine)
        worker = QCWorker(
            scope=_make_scope(sm),
            scope_for_tenant=_make_tenant_scope(sm),
            executor=_NeverInspects(),
            worker_id=f"loop-exp-{uuid.uuid4().hex[:8]}",
            lease_seconds=1,
        )
        scheduler = TenantScheduler(
            discovery=_ScopedDiscovery(engine=scheduler_engine, tenants={tenant_c}),
            worker=worker,
        )
        # 领取后立刻退出执行：任务留在 RUNNING，租约 1 秒后到期。
        assert await worker.process_one(tenant=tenant_c) is True
        row = await _status(admin_sessionmaker, task_c)
        assert row.status == "RUNNING"
        assert row.lease_owner

        # 租约是墙钟事实，这里必须等它真的过期，而不是把判断改成「epoch 算过」。
        await asyncio.sleep(1.2)
        # 过期租约仍在投影里（RUNNING + lease_expires_at <= now）——这正是
        # 「发现能触发恢复」的前提；若投影把它删了，清扫永远找不到它。
        reclaimed = await scheduler.sweep()
    finally:
        await scheduler_engine.dispose()
        await qc_engine.dispose()

    assert reclaimed == 1, "发现给出的过期租户必须被回收，且回收恰好一次"
    row = await _status(admin_sessionmaker, task_c)
    assert row.status == "QUEUED", "回收是把任务退回可领取，不是丢弃"
    assert not row.lease_owner
    assert await _projection_tenants(admin_sessionmaker, task_c) == 1, (
        "退回 QUEUED 后必须重新进入调度投影"
    )
