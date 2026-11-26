"""租户委派调度的单测：只钉「发现失败不等于队列为空」这条语义。

真实 PG 上「能不能只发现不读表」是授权面的事实，由集成用例证明；这里证明的是
控制流：发现失败必须**阻断**（不领取、不清扫），而不是当成空队列继续跑。
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
    def __init__(self, *, busy: bool = False, reclaimed: int = 0) -> None:
        self._busy = busy
        self._reclaimed = reclaimed
        self.processed: list = []
        self.swept: list = []

    async def process_one(self, *, tenant: object) -> bool:
        self.processed.append(tenant)
        return self._busy

    async def sweep_expired(self, *, tenant: object) -> int:
        self.swept.append(tenant)
        return self._reclaimed


async def test_dispatch_covers_each_tenant_reported_by_discovery() -> None:
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    worker = _Worker(busy=True)
    scheduler = TenantScheduler(discovery=_Discovery([tenant_a, tenant_b]), worker=worker)

    assert await scheduler.run_once() is True
    assert worker.processed == [tenant_a, tenant_b], (
        "每个被发现的租户都要被委派一次：漏掉一个租户就是永久饥饿"
    )
    assert worker.swept == [tenant_a, tenant_b]


async def test_discovery_failure_blocks_instead_of_claiming() -> None:
    worker = _Worker(busy=True)
    scheduler = TenantScheduler(discovery=_Discovery(None), worker=worker)

    assert await scheduler.run_once() is None, (
        "发现失败必须返回 None：与「空闲」的 False 共用一个出口，调用方只能误读"
    )
    assert worker.processed == [], "发现失败时不得领取：那会把停摆伪装成空队列"
    assert worker.swept == []

    assert await scheduler.sweep() is None, (
        "发现不可用时清扫必须返回 None（阻断），不能返回 0（看起来像清干净了）"
    )
    assert worker.swept == []


async def test_empty_discovery_is_a_clean_idle_cycle() -> None:
    worker = _Worker()
    scheduler = TenantScheduler(discovery=_Discovery([]), worker=worker)

    assert await scheduler.run_once() is False
    assert await scheduler.sweep() == 0
    assert worker.processed == [] and worker.swept == []


@pytest.mark.parametrize("busy", [True, False])
async def test_busy_flag_reflects_tenant_work(busy: bool) -> None:
    worker = _Worker(busy=busy)
    scheduler = TenantScheduler(discovery=_Discovery([uuid.uuid4()]), worker=worker)
    assert await scheduler.run_once() is busy
