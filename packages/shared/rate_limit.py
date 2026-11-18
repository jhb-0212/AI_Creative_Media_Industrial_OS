"""请求限流（Redis 固定窗口计数 + 明确的降级语义）。

为什么是固定窗口而不是令牌桶 / 滑动日志：
- 固定窗口只需 INCR + EXPIRE 两条原子命令，无 Lua、无本地状态，多副本天然
  一致；令牌桶要在 Redis 里存剩余令牌与时间戳并做 CAS，复杂度换来的平滑度
  对当前流量（内部平台、按分钟粒度）没有实际收益。

降级语义（刻意的取舍，不是遗漏）：
- Redis 不可用时 **放行**（fail-open）并计入 `rate_limit_backend_errors_total`。
  限流是保护性设施，不是正确性设施；让限流后端故障导致全站 503，是把可用性
  风险放大。放行 + 指标暴露，才能让运维看见「限流器事实上没在工作」。
- 严禁静默：每次降级都计数，错误日志由调用方打。

**为什么必须配熔断（不是可选的优化）**：fail-open 只回答「拒绝了怎么办」，
没回答「每次都要先撞一次后端」。Redis 地址配错或网络隔离时，每个业务请求都
要付一次完整的 ``socket_connect_timeout``（默认 1 秒）才降级——降级比不降级
更糟：``/health`` 2ms，而连 401 都要 1.01s，QPS 从数百掉到个位数。这不是把
超时调小能解决的：调小只是把 1 秒变成 0.1 秒，欠账仍在（Redis 抖动一次，
全站所有请求一起等）。真正的修法是**连续失败后短路**：熔断 OPEN 期间一个
网络包都不发，冷却期满放一个探测。前 N 次失败的代价是必要的探测成本，
之后归零。

限流键：已认证请求按令牌指纹（SHA-256 前 16 位）计数，未认证请求按客户端 IP。
- 不用 principal 做键：中间件跑在依赖之前，拿 principal 需要二次 RSA 验签，
  等于每请求多一次密码学开销；令牌指纹同样能隔离不同调用方，且无法伪造成
  他人的桶（伪造只会消耗自己的额度）。
- 按 IP 计数在 NAT / 反向代理后会退化成「所有外部用户共用一个桶」，
  部署在代理后必须让 uvicorn 采信可信代理的转发头（--proxy-headers），
  否则阈值要按「入口总流量」来配，而不是按单用户。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from redis.asyncio import Redis

from packages.shared.circuit_breaker import CircuitBreaker, CircuitState
from packages.shared.config import get_settings
from packages.shared.metrics import (
    RATE_LIMIT_BACKEND_ERRORS,
    RATE_LIMIT_CIRCUIT_STATE,
    observe_circuit_state,
)


def _observe_backend_circuit(
    state: CircuitState, previous: CircuitState | None
) -> None:
    """把限流后端熔断状态发布到限流专属 Gauge（不与 webhook 端点共用）。"""
    observe_circuit_state(
        RATE_LIMIT_CIRCUIT_STATE,
        state.value,
        previous.value if previous is not None else None,
    )


@dataclass(frozen=True)
class RateLimitDecision:
    """一次限流判定的结果。"""

    allowed: bool
    retry_after_seconds: int
    # degraded=True 表示限流后端不可用、已按 fail-open 放行。
    degraded: bool = False


class RateLimiter:
    """Redis 固定窗口限流器。

    ``client`` 为 None 或 ``enabled=False`` 时退化为「全部放行」，
    用于未配置 Redis 的环境（本地纯单元测试）而不改变调用方代码。
    """

    def __init__(
        self,
        client: Redis | None,
        *,
        limit: int,
        window_seconds: int,
        enabled: bool = True,
        breaker: CircuitBreaker | None = None,
    ) -> None:
        self._client = client
        self._limit = limit
        self._window_seconds = window_seconds
        self._enabled = enabled and client is not None
        self._breaker = breaker

    @classmethod
    def from_settings(cls) -> RateLimiter:
        """按配置构造：未启用时不起 Redis 连接。

        熔断阈值 0 表示关闭熔断（与 webhook 端点熔断的口径一致：用 0 表达关闭，
        而不是再加一个 enabled 开关——两个开关组合出四种状态，其中两种没有意义）。
        """
        settings = get_settings()
        if not settings.rate_limit_enabled:
            return cls(None, limit=0, window_seconds=0, enabled=False)
        client = Redis.from_url(
            settings.redis_url.get_secret_value(),
            decode_responses=True,
            socket_connect_timeout=1.0,
            socket_timeout=1.0,
        )
        breaker = CircuitBreaker(
            failure_threshold=settings.rate_limit_circuit_failure_threshold,
            recovery_timeout_seconds=settings.rate_limit_circuit_recovery_seconds,
            enabled=settings.rate_limit_circuit_failure_threshold > 0,
            state_observer=_observe_backend_circuit,
        )
        return cls(
            client,
            limit=settings.rate_limit_per_minute,
            window_seconds=settings.rate_limit_window_seconds,
            breaker=breaker,
        )

    async def acquire(self, identity: str) -> RateLimitDecision:
        """判定一次请求是否放行，并把判定写入指标（指标不含身份，避免高基数与泄漏）。"""
        if not self._enabled or self._client is None:
            return RateLimitDecision(allowed=True, retry_after_seconds=0)

        # 熔断 OPEN：一个网络包都不发，直接按 fail-open 放行。
        # 这里不增加 backend_errors：那个指标表示「真的尝试并失败了」，
        # 熔断期没有尝试，混进去会让「故障次数」失去意义。
        if self._breaker is not None and not self._breaker.allow():
            return RateLimitDecision(
                allowed=True, retry_after_seconds=0, degraded=True
            )

        key = f"ratelimit:{identity}"
        try:
            count = await self._client.incr(key)
            if count == 1:
                # 首次命中才设过期：每次都 EXPIRE 会把窗口无限延长成「永不重置」。
                await self._client.expire(key, self._window_seconds)
        except asyncio.CancelledError:
            # 请求被取消（服务关闭 / 调用方断开）：没有结论。半开期的在途标志
            # 必须显式释放，否则熔断器会永久判为「已有探测在途」而卡死。
            if self._breaker is not None:
                self._breaker.release_probe()
            raise
        except Exception:
            # 失败放行，但必须可观测——否则限流器形同虚设还无人知晓。
            RATE_LIMIT_BACKEND_ERRORS.inc()
            if self._breaker is not None:
                self._breaker.record_failure()
            return RateLimitDecision(
                allowed=True, retry_after_seconds=0, degraded=True
            )

        if self._breaker is not None:
            self._breaker.record_success()

        if count > self._limit:
            return RateLimitDecision(
                allowed=False, retry_after_seconds=self._window_seconds
            )
        return RateLimitDecision(allowed=True, retry_after_seconds=0)

    async def aclose(self) -> None:
        """关闭底层连接池。"""
        if self._client is not None:
            await self._client.aclose()
