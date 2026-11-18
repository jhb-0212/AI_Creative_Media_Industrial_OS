"""就绪探针的缓存、单飞与超时（/readyz 的成本控制）。

这些用例锁住三件容易被改坏的事：

1. **缓存真的省了探测**：断言的是「探测函数被调用了几次」，不是「响应一样」。
   只看响应无法区分「命中缓存」与「又真打了一次库但结果相同」——后者正是
   /readyz 每请求 90ms 的来源。
2. **并发只穿透一次**：缓存过期的瞬间 N 个请求同时到达，必须共享同一次探测。
3. **失败也缓存**：故障期探测最贵（等满超时）且系统最脆弱，不缓存会让探针
   成为压死连接池的最后一下。
"""

from __future__ import annotations

import asyncio

import pytest

from packages.shared.readiness import ReadinessProbe, ReadinessResult


class _Clock:
    """可手动拨动的假时钟，用于确定性驱动缓存过期。"""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _ready() -> ReadinessResult:
    return ReadinessResult(
        ready=True,
        checks={"project_planning_database": "up", "asset_service_database": "up"},
        schema_revision="b3c4d5e6f7a8",
    )


async def test_cached_result_avoids_repeat_probe() -> None:
    clock = _Clock()
    calls = 0

    async def check() -> ReadinessResult:
        nonlocal calls
        calls += 1
        return _ready()

    probe = ReadinessProbe(check, ttl_seconds=1.0, clock=clock)
    assert (await probe.probe()).ready is True
    assert (await probe.probe()).ready is True
    assert calls == 1  # 第二次是内存读，不是又一次三次往返


async def test_cache_expires_and_reprobes() -> None:
    clock = _Clock()
    calls = 0

    async def check() -> ReadinessResult:
        nonlocal calls
        calls += 1
        return _ready()

    probe = ReadinessProbe(check, ttl_seconds=1.0, clock=clock)
    await probe.probe()
    clock.advance(1.0)  # 恰好到期：视为过期（< 判定，不留半开边界）
    await probe.probe()
    assert calls == 2


async def test_zero_ttl_always_probes() -> None:
    """ttl=0 表示不缓存（排障/单测用），语义必须真的是「每次真探测」。"""
    calls = 0

    async def check() -> ReadinessResult:
        nonlocal calls
        calls += 1
        return _ready()

    probe = ReadinessProbe(check, ttl_seconds=0.0)
    await probe.probe()
    await probe.probe()
    assert calls == 2


async def test_concurrent_requests_share_single_probe() -> None:
    """缓存过期瞬间的并发穿透必须被单飞挡住。

    这是 thundering herd 的经典位置：N 个请求同时发现「缓存无效」，
    若各自启动探测，探测次数与请求数同阶——探针从观测者变成参与者。
    """
    gate = asyncio.Event()
    calls = 0

    async def check() -> ReadinessResult:
        nonlocal calls
        calls += 1
        await gate.wait()
        return _ready()

    probe = ReadinessProbe(check, ttl_seconds=10.0, timeout_seconds=5.0)
    tasks = [asyncio.create_task(probe.probe()) for _ in range(10)]
    await asyncio.sleep(0.01)  # 让 10 个请求都进入探测路径并卡在 gate
    gate.set()
    results = await asyncio.gather(*tasks)

    assert all(r.ready for r in results)
    assert calls == 1


async def test_failure_result_is_cached_too() -> None:
    """失败结果同样缓存：故障期不能让每个探针都去等一次超时。"""
    clock = _Clock()
    calls = 0

    async def check() -> ReadinessResult:
        nonlocal calls
        calls += 1
        return ReadinessResult.unready("project_planning_database")

    probe = ReadinessProbe(check, ttl_seconds=1.0, clock=clock)
    assert (await probe.probe()).ready is False
    assert (await probe.probe()).ready is False
    assert calls == 1

    clock.advance(1.1)
    await probe.probe()
    assert calls == 2  # 到期后才会重探（故障恢复最多迟 1 秒被发现）


async def test_timeout_reports_unready() -> None:
    """探测超时按 unready 处理（fail-closed），且不把等待时间传给调用方。"""

    async def slow() -> ReadinessResult:
        await asyncio.sleep(10)
        return _ready()

    probe = ReadinessProbe(slow, ttl_seconds=1.0, timeout_seconds=0.05)
    result = await probe.probe()
    assert result.ready is False
    assert result.checks == {"probe_timeout": "down"}


async def test_check_exception_reports_unready() -> None:
    """探测函数自身崩溃不能变成 500：编排系统要的是「能不能接流量」的答案。"""

    async def boom() -> ReadinessResult:
        raise RuntimeError("driver exploded")

    probe = ReadinessProbe(boom, ttl_seconds=1.0, timeout_seconds=1.0)
    result = await probe.probe()
    assert result.ready is False
    assert result.checks == {"probe_failed": "down"}


async def test_cancellation_is_not_cached() -> None:
    """取消不是探测结论，不得写入缓存。

    若把「被取消」当成一次探测结果缓存下来，一次无关的取消会让接下来 1 秒内
    所有请求拿到同样的错误答案——比不缓存更糟。
    """
    calls = 0

    async def check() -> ReadinessResult:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise asyncio.CancelledError
        return _ready()

    probe = ReadinessProbe(check, ttl_seconds=10.0, timeout_seconds=1.0)
    with pytest.raises(asyncio.CancelledError):
        await probe.probe()

    # 缓存未被污染：下个请求重新探测并拿到真实结果。
    assert (await probe.probe()).ready is True
    assert calls == 2


def test_constructor_rejects_invalid_bounds() -> None:
    """非法参数必须在构造期失败，而不是静默退化成「永不缓存/永不超时」。."""

    async def check() -> ReadinessResult:
        return _ready()

    with pytest.raises(ValueError):
        ReadinessProbe(check, ttl_seconds=-1)
    with pytest.raises(ValueError):
        ReadinessProbe(check, timeout_seconds=0)
