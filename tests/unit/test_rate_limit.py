"""限流语义与 /metrics 端点测试。

覆盖三件事（都是“能被改坏就变红”的断言）：
1. 窗口内超过上限拒绝，返回 429 + Retry-After + 统一错误信封。
2. 限流后端故障时 fail-open（放行）且计入降级指标——不允许静默失效。
3. /metrics 真实暴露指标文本，不是空壳端点。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from apps.api.main import app
from packages.shared.circuit_breaker import CircuitBreaker, CircuitState
from packages.shared.metrics import RATE_LIMIT_BACKEND_ERRORS
from packages.shared.rate_limit import RateLimiter


class _FakeRedis:
    """最小 Redis 替身：只实现限流用到的 incr/expire。"""

    def __init__(self, *, fail: bool = False) -> None:
        self._values: dict[str, int] = {}
        self._ttl: dict[str, int] = {}
        self._fail = fail

    async def incr(self, key: str) -> int:
        if self._fail:
            raise ConnectionError("redis down")
        self._values[key] = self._values.get(key, 0) + 1
        return self._values[key]

    async def expire(self, key: str, seconds: int) -> bool:
        if self._fail:
            raise ConnectionError("redis down")
        self._ttl[key] = seconds
        return True

    async def aclose(self) -> None:
        return None


@pytest.fixture
def client() -> TestClient:
    previous = getattr(app.state, "rate_limiter", None)
    with TestClient(app) as c:
        yield c
    app.state.rate_limiter = previous


def _limiter(limit: int, *, fail: bool = False) -> RateLimiter:
    return RateLimiter(
        _FakeRedis(fail=fail),  # type: ignore[arg-type]
        limit=limit,
        window_seconds=60,
    )


async def test_within_limit_is_allowed() -> None:
    limiter = _limiter(2)
    assert (await limiter.acquire("ip-1")).allowed
    assert (await limiter.acquire("ip-1")).allowed


async def test_exceeding_limit_is_denied_with_retry_after() -> None:
    limiter = _limiter(1)
    await limiter.acquire("ip-2")
    decision = await limiter.acquire("ip-2")
    assert decision.allowed is False
    assert decision.retry_after_seconds == 60
    # 不同身份各自独立计数（窗口按 key 隔离）。
    assert (await limiter.acquire("ip-3")).allowed


async def test_backend_failure_fails_open_and_is_counted() -> None:
    """限流后端故障必须放行并记降级，绝不允许静默 503。"""
    limiter = _limiter(1, fail=True)
    decision = await limiter.acquire("ip-4")
    assert decision.allowed is True
    assert decision.degraded is True


def test_rate_limit_middleware_returns_429_envelope(client: TestClient) -> None:
    """端到端：超过上限时由中间件返回 429，且带统一信封与 Retry-After。"""
    app.state.rate_limiter = _limiter(1)

    # 首次放行（未带凭证 → 401，但证明限流没有提前拦截）。
    payload = {"project_name": "限流探针", "project_tier": "B"}
    first = client.post("/projects", json=payload)
    assert first.status_code == 401

    second = client.post("/projects", json=payload)
    assert second.status_code == 429
    body = second.json()
    assert body["error_code"] == "RATE_LIMIT"
    assert body["retryable"] is True
    assert second.headers["Retry-After"] == "60"


def test_metrics_endpoint_exposes_counters(client: TestClient) -> None:
    resp = client.get("/metrics")
    assert resp.status_code == 200
    text = resp.text
    assert "http_requests_total" in text
    assert "rate_limit_decisions_total" in text
    assert "rate_limit_backend_errors_total" in text
    # 熔断状态必须被真实发布：熔断器只在构造与迁移时回调观察者，
    # 若这行缺失，说明观察者没接上——指标名存在但永远是空的。
    assert 'rate_limit_circuit_state{state="closed"} 1.0' in text


def test_token_based_buckets_are_independent(client: TestClient) -> None:
    """不同凭证各自一个桶：一个被限死不能连累另一个。

    这条同时锁住键的构造方式——若有人把 identity 改回「只按 IP」，
    在同一进程内两个 token 会共用一个计数，本用例立刻变红。
    """
    app.state.rate_limiter = _limiter(1)
    payload = {"project_name": "限流探针", "project_tier": "B"}
    headers_a = {"Authorization": "Bearer token-a"}
    headers_b = {"Authorization": "Bearer token-b"}

    assert client.post("/projects", json=payload, headers=headers_a).status_code == 401
    assert client.post("/projects", json=payload, headers=headers_a).status_code == 429
    assert client.post("/projects", json=payload, headers=headers_b).status_code == 401


# --------------------------------------------------------------------------- #
# 限流后端熔断（P0：Redis 不可达时每请求阻塞一个连接超时）
# --------------------------------------------------------------------------- #
class _CountingRedis:
    """可裁剪响应脚本的 Redis 替身：精确统计调用次数与逐次行为。

    熔断要证明的恰恰是「短路后不再调用」——只看决策结果看不出这一点，
    必须数调用次数（否则「调用了但很快失败」也会被误判为达标）。
    """

    def __init__(self, *behaviours: object) -> None:
        self._queue = list(behaviours)
        self.calls = 0
        self._fail = False

    async def incr(self, key: str) -> int:
        self.calls += 1
        if self._queue:
            item = self._queue.pop(0)
            if isinstance(item, BaseException):
                raise item
            return int(item)  # type: ignore[arg-type]
        if self._fail:
            raise ConnectionError("redis down")
        return 1

    async def expire(self, key: str, seconds: int) -> bool:
        return True

    async def aclose(self) -> None:
        return None


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _counter_value(counter: object) -> float:
    return float(counter._value.get())  # type: ignore[attr-defined]


async def test_circuit_opens_and_stops_calling_backend() -> None:
    """连续失败达阈值后短路：不再发网络请求。

    这是「每请求固定阻塞一秒」的根治点。只断言「仍然放行」是不充分的
    ——fail-open 一直都放行，问题在于每次都要先撞一次后端；必须断言调用次数。
    """
    redis = _CountingRedis()
    redis._fail = True
    breaker = CircuitBreaker(failure_threshold=2, recovery_timeout_seconds=30.0)
    limiter = RateLimiter(
        redis,  # type: ignore[arg-type]
        limit=10,
        window_seconds=60,
        breaker=breaker,
    )

    for _ in range(2):
        decision = await limiter.acquire("ip-c1")
        assert decision.allowed is True
        assert decision.degraded is True
    assert breaker.state is CircuitState.OPEN

    calls_before = redis.calls
    decision = await limiter.acquire("ip-c1")
    assert decision.allowed is True  # 仍然 fail-open
    assert decision.degraded is True
    assert redis.calls == calls_before  # 短路：一个网络包都没发


async def test_circuit_open_does_not_inflate_backend_errors() -> None:
    """熔断期不增加 backend_errors：那个指标表示「真的尝试并失败了」。

    混进去会让「后端故障次数」失去意义，运维无法区分「Redis 挂了」与
    「熔断器在正常工作」。
    """
    redis = _CountingRedis()
    redis._fail = True
    breaker = CircuitBreaker(failure_threshold=1, recovery_timeout_seconds=30.0)
    limiter = RateLimiter(
        redis,  # type: ignore[arg-type]
        limit=10,
        window_seconds=60,
        breaker=breaker,
    )

    await limiter.acquire("ip-c2")  # 真失败一次
    after_failure = _counter_value(RATE_LIMIT_BACKEND_ERRORS)
    assert breaker.state is CircuitState.OPEN

    await limiter.acquire("ip-c3")  # 熔断短路
    assert _counter_value(RATE_LIMIT_BACKEND_ERRORS) == after_failure


async def test_half_open_probe_recovers_circuit() -> None:
    """冷却期满放行一个探测；探测成功即关闭熔断，恢复正常限流语义。"""
    clock = _Clock()
    redis = _CountingRedis(ConnectionError("down"))
    breaker = CircuitBreaker(
        failure_threshold=1, recovery_timeout_seconds=10.0, clock=clock
    )
    limiter = RateLimiter(
        redis,  # type: ignore[arg-type]
        limit=1,
        window_seconds=60,
        breaker=breaker,
    )

    assert (await limiter.acquire("ip-c4")).degraded is True
    assert breaker.state is CircuitState.OPEN

    clock.advance(11)
    redis._queue.append(1)  # 后端恢复
    decision = await limiter.acquire("ip-c4")
    assert decision.allowed is True
    assert decision.degraded is False
    assert breaker.state is CircuitState.CLOSED

    # 恢复后重新按阈值判定（不再是「一律放行」）。
    redis._queue.append(2)
    denied = await limiter.acquire("ip-c4")
    assert denied.allowed is False
    assert denied.retry_after_seconds == 60


async def test_cancelled_request_releases_half_open_probe() -> None:
    """探测请求被取消（服务关闭/调用方断开）不能让熔断器永久卡在半开。"""
    import asyncio

    clock = _Clock()
    redis = _CountingRedis(ConnectionError("down"), asyncio.CancelledError())
    breaker = CircuitBreaker(
        failure_threshold=1, recovery_timeout_seconds=5.0, clock=clock
    )
    limiter = RateLimiter(
        redis,  # type: ignore[arg-type]
        limit=10,
        window_seconds=60,
        breaker=breaker,
    )

    await limiter.acquire("ip-c5")  # 失败 → OPEN
    clock.advance(6)  # 冷却期满 → 下一个请求是半开探测

    with pytest.raises(asyncio.CancelledError):
        await limiter.acquire("ip-c5")

    assert breaker.state is CircuitState.HALF_OPEN
    # 在途标志已释放：下一个请求仍能探测（否则这里会是 False，永久卡死）。
    assert breaker.allow() is True
