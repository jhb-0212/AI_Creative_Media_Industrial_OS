"""按端点（endpoint）的熔断器（Circuit Breaker）。

为什么需要它、以及它和「退避 + 死信」的分层关系，见
``docs/adr/ADR-R13-outbox-webhook-circuit-breaker.md``。一句话：

- 事件级「指数退避 + 死信」解决的是**单条事件**失败后不再无限重试；
- 端点级「熔断」解决的是**下游整体故障**时，新事件不再逐条撞墙、放大请求。

三态语义（标准熔断器状态机）：

- ``CLOSED``：正常放行。连续失败达到 ``failure_threshold`` 后进入 ``OPEN``。
- ``OPEN``：快速失败（不真正发请求），``recovery_timeout`` 秒后进入 ``HALF_OPEN``。
- ``HALF_OPEN``：只放行**一个**探测请求；成功回 ``CLOSED``，失败回 ``OPEN``
  并重新计时。

设计取舍（刻意的，不是遗漏）：

1. **进程内、无共享存储**：熔断状态只在本 relay 副本内。多副本各自维护，
   下游故障时每个副本都独立进入 OPEN——这已经足够（目标是「别持续放大请求」），
   不需要引入 Redis/分布式锁来共享熔断状态。跨副本的精确一致由 Prometheus
   聚合指标 + 告警承担。
2. **时钟注入**：``clock`` 参数默认为 ``time.monotonic``。冷却期是「过了多久」
   的度量，不能用墙钟：NTP 校时或人工改时间回拨 30 秒，``clock() - opened_at``
   变负，冷却期永远不到期，熔断器会卡死在 OPEN——即「下游恢复了但系统永远
   不再试」。测试注入假时钟即可确定性驱动时序，不依赖真实 sleep。
3. **半开探测的原子性**：``allow()`` 从读状态到改写状态之间无 ``await``，
   asyncio 单线程内天然原子，不需要额外加锁；「只放行一个」用布尔标志实现。
4. **指标由使用方注入**：熔断器是通用设施，不替使用方决定指标名。
   ``state_observer`` 为空时完全不写指标（本模块不 import 任何业务指标），
   使用方各自注入自己的 Gauge——限流后端状态与 webhook 端点状态是两件事，
   共用一个指标名会让「哪个组件在熔断」变得不可分辨。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from enum import Enum


class CircuitState(Enum):
    """熔断器三态。"""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """连续失败阈值 + 冷却期半开探测的熔断器。

    ``enabled=False`` 时退化为「永远放行」：用于未配置 webhook 或显式关闭熔断的
    环境，而不改变调用方代码（与 ``RateLimiter`` 的退化约定一致）。

    关闭态的**参数口径**（审查 N1）：关闭时阈值/冷却期不参与任何判定，配置层用
    「阈值 0」表达关闭（见 ``packages/shared/config.py``），因此这里在
    ``enabled=False`` 时不做参数校验，并把阈值归一化到 >=1 保存。
    反过来，``enabled=True`` 时参数非法一律 ValueError——开着熔断却给了不可能
    生效的阈值，是配置错误，必须启动即失败，不能静默退化成「永不熔断」。
    """

    def __init__(
        self,
        *,
        failure_threshold: int = 5,
        recovery_timeout_seconds: float = 30.0,
        enabled: bool = True,
        clock: Callable[[], float] | None = None,
        state_observer: Callable[[CircuitState, CircuitState | None], None] | None = None,
    ) -> None:
        if enabled:
            if failure_threshold < 1:
                raise ValueError("failure_threshold 必须 >= 1")
            if recovery_timeout_seconds <= 0:
                raise ValueError("recovery_timeout_seconds 必须 > 0")
        # 关闭态：阈值不参与判定，但仍归一化到 >=1 保存，避免把 0/负数带进
        # 后续比较；冷却期同样不参与判定（allow 在关闭时直接放行），原样保留。
        self._failure_threshold = max(1, failure_threshold)
        self._recovery_timeout = recovery_timeout_seconds
        self._enabled = enabled
        # 默认单调时钟：冷却期的语义是「过了多久」，墙钟回拨会让它永不到期。
        self._clock = clock if clock is not None else time.monotonic
        self._state_observer = state_observer

        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._opened_at = 0.0
        # HALF_OPEN 时「已放行一个探测」标志：探测未出结果前不再放行第二个。
        self._probe_in_flight = False
        self._emit(None)

    @property
    def state(self) -> CircuitState:
        """当前状态（读操作，不触发状态迁移）。"""
        return self._state

    @property
    def enabled(self) -> bool:
        """是否启用熔断（关闭时 ``allow()`` 永远放行、状态恒为 CLOSED）。"""
        return self._enabled

    def allow(self) -> bool:
        """是否放行当前请求；CLOSED/HALF_OPEN（且无在途探测）放行，OPEN 快速失败。

        此方法可能触发「OPEN 冷却期满 → HALF_OPEN」的状态迁移（惰性迁移：
        只在下次请求到来时判定，不额外起定时器）。
        """
        if not self._enabled:
            return True

        if self._state is CircuitState.OPEN:
            # 惰性迁移：冷却期满才进入半开，否则保持快速失败。
            if self._clock() - self._opened_at >= self._recovery_timeout:
                self._transition(CircuitState.HALF_OPEN)
            else:
                return False

        if self._state is CircuitState.HALF_OPEN:
            if self._probe_in_flight:
                # 半开期只允许一个探测在途；其余请求继续快速失败。
                return False
            self._probe_in_flight = True
            return True

        # CLOSED：正常放行。
        return True

    def record_failure(self) -> None:
        """记录一次失败；达到阈值转 OPEN，半开失败回 OPEN 并重新计时。"""
        if not self._enabled:
            return

        if self._state is CircuitState.HALF_OPEN:
            # 探测失败：立即回 OPEN，重新计冷却期。
            self._probe_in_flight = False
            self._transition(CircuitState.OPEN)
            return

        self._consecutive_failures += 1
        if self._consecutive_failures >= self._failure_threshold:
            self._transition(CircuitState.OPEN)

    def record_success(self) -> None:
        """记录一次成功；连续失败清零，半开探测成功回 CLOSED。"""
        if not self._enabled:
            return

        if self._state is CircuitState.HALF_OPEN:
            self._probe_in_flight = False
            self._transition(CircuitState.CLOSED)
            return

        self._consecutive_failures = 0

    def release_probe(self) -> None:
        """放弃在途探测（请求被取消，未得到任何结论）。

        半开期放行一个探测后，若该请求被取消（服务关闭、调用方断开），
        ``record_success`` 与 ``record_failure`` 都不会被调用，``_probe_in_flight``
        会永远为 True——此后所有请求都被判为「半开期已有探测在途」而快速失败，
        熔断器永久卡死。取消既不是成功也不是失败，正确语义是「回到未探测」，
        让下一个请求重新探测。
        """
        if self._state is CircuitState.HALF_OPEN:
            self._probe_in_flight = False

    def _transition(self, new_state: CircuitState) -> None:
        """状态迁移的唯一入口：统一维护伴随字段与指标，避免各分支各自散改。"""
        previous = self._state
        self._state = new_state
        if new_state is CircuitState.OPEN:
            self._opened_at = self._clock()
            self._consecutive_failures = 0
        elif new_state is CircuitState.CLOSED:
            self._consecutive_failures = 0
            self._probe_in_flight = False
        elif new_state is CircuitState.HALF_OPEN:
            # 半开不重置 opened_at（保留原始故障时间供观测），只清在途标志。
            self._probe_in_flight = False
        self._emit(previous)

    def _emit(self, previous: CircuitState | None) -> None:
        """把「从 previous 迁移到当前态」交给使用方注入的观察者。

        本模块刻意不 import 任何指标：熔断器不知道自己被谁用、也不知道使用者
        想把状态记到哪个指标上。使用方注入 ``state_observer`` 后自行决定
        （webhook 写端点熔断 Gauge、限流写限流后端 Gauge）。

        传 ``previous`` 而不是让观察者遍历全部状态：只有真发生迁移时才需要
        「清旧置新」，metrics 层因此不需要再维护一份状态名清单（那是第二个
        事实源，新增状态时必定漏改）。
        """
        if self._state_observer is not None:
            self._state_observer(self._state, previous)
