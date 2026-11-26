"""租户委派调度的单测：钉住三条控制流语义。

真实 PG 上「能不能只发现不读表」是授权面的事实，由集成用例证明；这里证明的是
控制流：

* 发现失败必须**阻断**（不领取、不清扫），而不是当成空队列继续跑；
* 空发现是**干净空闲**，不能与阻断共用一个返回值；
* 单租户失败**不连坐**其他租户，但也不能被折叠成「空闲」。
"""

from __future__ import annotations

import uuid

import pytest

from workers.qc.scheduler import TenantScheduler


class _Discovery:
    def __init__(self, tenants: list | None) -> None:
        self._tenants = tenants
        self.calls = 0

    async def tenants(self) -> list | None:
        self.calls += 1
        return self._tenants


class _Worker:
    def __init__(
        self, *, busy: bool = False, reclaimed: int = 0, fail_for: set | None = None
    ) -> None:
        self._busy = busy
        self._reclaimed = reclaimed
        self._fail_for = fail_for or set()
        self.processed: list = []
        self.swept: list = []

    async def process_one(self, *, tenant: object) -> bool:
        self.processed.append(tenant)
        if tenant in self._fail_for:
            raise RuntimeError(f"boom for {tenant}")
        return self._busy

    async def sweep_expired(self, *, tenant: object) -> int:
        self.swept.append(tenant)
        if tenant in self._fail_for:
            raise RuntimeError(f"boom sweep for {tenant}")
        return self._reclaimed


async def test_dispatch_covers_each_tenant_reported_by_discovery() -> None:
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    worker = _Worker(busy=True)
    scheduler = TenantScheduler(discovery=_Discovery([tenant_a, tenant_b]), worker=worker)

    outcome = await scheduler.run_once()
    assert outcome.busy is True
    assert outcome.blocked is False and outcome.tenant_errors == 0
    assert worker.processed == [tenant_a, tenant_b], (
        "每个被发现的租户都要被委派一次：漏掉一个租户就是永久饥饿"
    )
    assert worker.swept == [tenant_a, tenant_b]


async def test_discovery_failure_blocks_instead_of_claiming() -> None:
    worker = _Worker(busy=True)
    scheduler = TenantScheduler(discovery=_Discovery(None), worker=worker)

    outcome = await scheduler.run_once()
    assert outcome.blocked is True, (
        "发现失败必须与「空闲」分开：共用一个返回值，调用方只能误读"
    )
    assert outcome.busy is False and outcome.tenant_errors == 0
    assert worker.processed == [], "发现失败时不得领取：那会把停摆伪装成空队列"
    assert worker.swept == []

    assert await scheduler.sweep() is None, (
        "发现不可用时清扫必须返回 None（阻断），不能返回 0（看起来像清干净了）"
    )
    assert worker.swept == []


async def test_empty_discovery_is_a_clean_idle_cycle() -> None:
    worker = _Worker()
    scheduler = TenantScheduler(discovery=_Discovery([]), worker=worker)

    outcome = await scheduler.run_once()
    assert outcome.blocked is False, "空发现是健康事实，不是阻断"
    assert outcome.busy is False and outcome.tenant_errors == 0
    assert await scheduler.sweep() == 0
    assert worker.processed == [] and worker.swept == []


async def test_one_broken_tenant_does_not_starve_the_others() -> None:
    broken, healthy = uuid.uuid4(), uuid.uuid4()
    worker = _Worker(busy=True, fail_for={broken})
    scheduler = TenantScheduler(
        discovery=_Discovery([broken, healthy]), worker=worker
    )

    outcome = await scheduler.run_once()

    assert outcome.tenant_errors == 1, "部分失败必须作为 degraded 上报"
    assert outcome.busy is True, "健康租户仍应被调度"
    assert worker.processed == [broken, healthy], (
        "坏租户排在前面也不能挡住后面的租户：逐租户隔离正是为了不让一处失败"
        "冻结整批"
    )


async def test_all_tenants_failing_is_degraded_not_idle() -> None:
    broken = uuid.uuid4()
    worker = _Worker(busy=True, fail_for={broken})
    scheduler = TenantScheduler(discovery=_Discovery([broken]), worker=worker)

    outcome = await scheduler.run_once()

    assert outcome.busy is False
    assert outcome.degraded is True, (
        "全部租户都失败时 busy=False；若不看 tenant_errors，一轮全败会被读成"
        "「队列空闲」"
    )


@pytest.mark.parametrize("busy", [True, False])
async def test_busy_flag_reflects_tenant_work(busy: bool) -> None:
    worker = _Worker(busy=busy)
    scheduler = TenantScheduler(discovery=_Discovery([uuid.uuid4()]), worker=worker)
    outcome = await scheduler.run_once()
    assert outcome.busy is busy
    assert outcome.tenant_errors == 0
