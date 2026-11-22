"""就绪探针的缓存与单飞（/readyz 的成本控制）。

## 为什么需要这一层

``/readyz`` 要回答的是「这个实例现在能不能接流量」，答案来自真实依赖：
两个域连接池各做一次 ``SELECT 1``，再读一次 ``alembic_version``。单次约 90ms
（三次连接获取 + 三次往返）。

探针是**编排面**端点，调用频率与业务流量无关：K8s 每 10s 一次、负载均衡每 5s
一次、监控再抓一次，都很正常。问题不在单次 90ms，而在两个放大效应：

1. **频率放大**：探针调用次数取决于编排配置，不取决于负载。业务空闲时它照跑。
2. **并发放大**：N 个探针同时到达时，每个都去抢域连接池。业务把池占满时，
   探针排队等连接，等到超时被判 unready；而探针自己的排队又加剧了池的紧张——
   探针从「观测者」变成了「参与者」。

所以探针必须做到：**探测频率与请求频率解耦**（TTL 缓存）、**同一时刻只有一次
真实探测**（单飞）、**探测本身有上限**（超时）。

## 为什么失败结果也要缓存

这是最容易做错的一处。直觉上「失败要立刻重探才知道恢复没有」，实际相反：
失败时的探测**最贵**（要等满超时），而这时系统正处在最脆弱的状态。不缓存失败
结果，等于在数据库挂掉时让每个探针请求都去等 2 秒并占一个连接槽——探针会成为
把连接池彻底压死的那一下。

缓存时长对恢复判断的影响可忽略：TTL 1s，而编排系统的摘流/恢复传播本来就是
秒级到十秒级。用「最多迟 1 秒发现恢复」换「故障期间探针零成本」，是明确的划算。

## 为什么单飞不能只靠 try/finally

缓存过期的瞬间，N 个等待中的请求会同时发现「缓存无效」，若各自启动探测就是
N 次穿透（thundering herd）。单飞要求它们共享同一个在途任务。

清理在途标记必须挂在任务完成回调上，而不是发起者的 ``finally``：发起者可能被
取消（客户端断开），此时任务还在跑，``finally`` 会把标记清掉，下一个请求于是
再起一个任务——两个探测并发，单飞失效。

## 时钟

缓存年龄用 ``time.monotonic``：这是「过了多久」，墙钟回拨会让缓存永不过期
（或立刻过期），NTP 校时就足以触发。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from packages.shared.logging import get_logger

logger = get_logger("packages.shared.readiness")


@dataclass(frozen=True)
class ReadinessResult:
    """一次就绪判定的结果（可直接序列化为 /readyz 响应体）。

    ``checks`` 记录各依赖的判定，用于让运维一眼看出「哪个池挂了」，而不是只
    看到一个笼统的 unready。它不含连接串、角色名或底层异常——这是编排面协议，
    不是排障详情页，泄漏面要小。
    """

    ready: bool
    checks: dict[str, str] = field(default_factory=dict)
    schema_revision: str | None = None

    @classmethod
    def unready(cls, reason: str) -> ReadinessResult:
        """构造失败结果：``reason`` 作为 check 名（如 ``probe_timeout``）。"""
        return cls(ready=False, checks={reason: "down"})


class ReadinessProbe:
    """带 TTL 缓存与单飞的就绪探测器。

    ``check`` 返回一次真实探测的结果；可抛异常，异常由本类兜成 unready
    （探针自身崩溃不能表现为 500——编排系统要的是「能不能接流量」这个问题
    的答案，不是一份堆栈）。

    ``ttl_seconds=0`` 表示不缓存（每次真探测）。测试与排障时有用，生产默认 1s。
    """

    def __init__(
        self,
        check: Callable[[], Awaitable[ReadinessResult]],
        *,
        ttl_seconds: float = 1.0,
        timeout_seconds: float = 2.0,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if ttl_seconds < 0:
            raise ValueError("ttl_seconds 不能为负")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds 必须 > 0")
        self._check = check
        self._ttl = ttl_seconds
        self._timeout = timeout_seconds
        self._clock = clock if clock is not None else time.monotonic
        self._cached: ReadinessResult | None = None
        self._cached_at = 0.0
        self._inflight: asyncio.Task[ReadinessResult] | None = None

    async def probe(self) -> ReadinessResult:
        """返回就绪结果：缓存有效则直接返回，否则共用一次真实探测。"""
        if self._cached is not None and self._clock() - self._cached_at < self._ttl:
            return self._cached

        task = self._inflight
        if task is None:
            task = asyncio.ensure_future(self._probe_once())
            self._inflight = task
            task.add_done_callback(self._release_inflight)
        return await task

    def _release_inflight(self, task: asyncio.Task[ReadinessResult]) -> None:
        """任务结束后清在途标记（只清自己那一个，防止覆盖后来者）。"""
        if self._inflight is task:
            self._inflight = None

    async def _probe_once(self) -> ReadinessResult:
        """执行一次真实探测并写入缓存（成功与失败都缓存）。"""
        try:
            result = await asyncio.wait_for(self._check(), timeout=self._timeout)
        except TimeoutError:
            # 超时通常意味着连接池耗尽或网络黑洞：这正是「不能接流量」。
            logger.warning("readiness_probe_timeout", timeout_seconds=self._timeout)
            result = ReadinessResult.unready("probe_timeout")
        except asyncio.CancelledError:
            # 取消不是探测结论，不能写进缓存（否则一次取消会冻结 1 秒的错误答案）。
            raise
        except Exception:
            logger.error("readiness_probe_failed", exc_info=True)
            result = ReadinessResult.unready("probe_failed")

        self._cached = result
        self._cached_at = self._clock()
        return result
