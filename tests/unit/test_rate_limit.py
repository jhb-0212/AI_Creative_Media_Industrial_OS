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
