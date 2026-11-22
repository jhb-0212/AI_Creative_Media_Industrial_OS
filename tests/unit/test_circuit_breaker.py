"""熔断器状态机单测（R9）。

覆盖三态迁移与半开探测的完整生命周期，用假时钟驱动「冷却期满」时序，
不依赖真实 sleep。关键断言：

1. 连续失败达阈值 → OPEN，之后快速失败（不真正放行）；
2. 冷却期满 → HALF_OPEN，只放行一个探测；
3. 探测成功 → CLOSED（连续失败清零）；探测失败 → 回 OPEN 重新计时。
"""

from __future__ import annotations

import pytest

from packages.shared.circuit_breaker import CircuitBreaker, CircuitState


class _FakeClock:
    """可手动拨动的假时钟，用于确定性驱动冷却期。"""

    def __init__(self) -> None:
        self._now = 0.0

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


def test_closed_allows_until_threshold_then_opens() -> None:
    breaker = CircuitBreaker(failure_threshold=3)

    # 前 2 次失败仍在 CLOSED，继续放行。
    for _ in range(2):
        assert breaker.allow() is True
        breaker.record_failure()
    assert breaker.state is CircuitState.CLOSED

    # 第 3 次失败触发 OPEN。
    assert breaker.allow() is True
    breaker.record_failure()
    assert breaker.state is CircuitState.OPEN


def test_open_fast_fails_without_allowing() -> None:
    breaker = CircuitBreaker(failure_threshold=1)
    assert breaker.allow() is True
    breaker.record_failure()
    assert breaker.state is CircuitState.OPEN

    # OPEN 期间一律快速失败。
    for _ in range(5):
        assert breaker.allow() is False


def test_open_recovers_to_half_open_after_cooldown() -> None:
    clock = _FakeClock()
    breaker = CircuitBreaker(
        failure_threshold=1, recovery_timeout_seconds=30.0, clock=clock
    )
    assert breaker.allow() is True
    breaker.record_failure()
    assert breaker.state is CircuitState.OPEN

    # 冷却期内仍是 OPEN 快速失败。
    clock.advance(29.0)
    assert breaker.allow() is False
    assert breaker.state is CircuitState.OPEN

    # 冷却期满 → HALF_OPEN，放行一个探测。
    clock.advance(1.0)
    assert breaker.allow() is True
    assert breaker.state is CircuitState.HALF_OPEN


def test_half_open_only_allows_one_probe_in_flight() -> None:
    clock = _FakeClock()
    breaker = CircuitBreaker(
        failure_threshold=1, recovery_timeout_seconds=10.0, clock=clock
    )
    breaker.allow()
    breaker.record_failure()  # → OPEN
    clock.advance(10.0)

    # 第一个请求成为探测（放行），探测未出结果前不再放行第二个。
    assert breaker.allow() is True
    assert breaker.allow() is False
    assert breaker.allow() is False


def test_half_open_probe_success_closes() -> None:
    clock = _FakeClock()
    breaker = CircuitBreaker(
        failure_threshold=1, recovery_timeout_seconds=10.0, clock=clock
    )
    breaker.allow()
    breaker.record_failure()  # → OPEN
    clock.advance(10.0)

    assert breaker.allow() is True  # 半开探测
    breaker.record_success()
    assert breaker.state is CircuitState.CLOSED

    # CLOSED 后恢复放行。
    assert breaker.allow() is True


def test_half_open_probe_failure_reopens_with_fresh_timer() -> None:
    clock = _FakeClock()
    breaker = CircuitBreaker(
        failure_threshold=1, recovery_timeout_seconds=10.0, clock=clock
    )
    breaker.allow()
    breaker.record_failure()  # → OPEN
    clock.advance(10.0)

    assert breaker.allow() is True  # 半开探测
    breaker.record_failure()  # 探测失败 → 回 OPEN
    assert breaker.state is CircuitState.OPEN

    # 重新计时：立即放行应为 False。
    assert breaker.allow() is False
    # 再等满冷却期才能半开。
    clock.advance(10.0)
    assert breaker.allow() is True
    assert breaker.state is CircuitState.HALF_OPEN


def test_success_resets_consecutive_failures_in_closed() -> None:
    breaker = CircuitBreaker(failure_threshold=3)
    for _ in range(2):
        assert breaker.allow() is True
        breaker.record_failure()
    # 一次成功清零连续失败计数。
    breaker.record_success()
    # 再失败 2 次（不是 1 次）才触发 OPEN。
    assert breaker.allow() is True
    breaker.record_failure()
    assert breaker.state is CircuitState.CLOSED
    assert breaker.allow() is True
    breaker.record_failure()
    assert breaker.state is CircuitState.CLOSED
    assert breaker.allow() is True
    breaker.record_failure()
    assert breaker.state is CircuitState.OPEN


def test_disabled_always_allows() -> None:
    breaker = CircuitBreaker(failure_threshold=1, enabled=False)
    for _ in range(10):
        assert breaker.allow() is True
        breaker.record_failure()
    assert breaker.state is CircuitState.CLOSED
    assert breaker.enabled is False


def test_invalid_parameters_rejected_when_enabled() -> None:
    """熔断开启时参数非法必须启动即失败（不能静默退化成「永不熔断」）。"""
    with pytest.raises(ValueError):
        CircuitBreaker(failure_threshold=0)
    with pytest.raises(ValueError):
        CircuitBreaker(recovery_timeout_seconds=0)
    with pytest.raises(ValueError):
        CircuitBreaker(recovery_timeout_seconds=-1)


def test_threshold_zero_means_disabled_and_is_constructible() -> None:
    """配置口径「阈值 0 = 关闭熔断」必须真的可构造（审查 N1）。

    relay 入口按 ``threshold > 0`` 派生 ``enabled``，同时把原值 0 原样传给
    dispatcher/breaker；若构造期先校验阈值再判断开关，文档写的关闭手段会让
    进程启动崩溃。
    """
    breaker = CircuitBreaker(failure_threshold=0, enabled=False)
    assert breaker.enabled is False
    assert breaker.state is CircuitState.CLOSED
    for _ in range(10):
        assert breaker.allow() is True
        breaker.record_failure()
    assert breaker.state is CircuitState.CLOSED
    # 归一化后阈值不低于 1：关闭态也不会把非法值带进后续比较。
    assert breaker._failure_threshold >= 1


# --------------------------------------------------------------------------- #
# 状态观察者（指标注入）与在途探测释放
# --------------------------------------------------------------------------- #
def test_state_observer_receives_transitions_with_previous() -> None:
    """观察者收到 (新态, 旧态)，首次发布 previous 为 None。

    这个签名是有意的：只有真发生迁移时才需要「清旧置新」，metrics 层因此
    不必自己维护一份状态名清单（那是第二个事实源，新增状态时必定漏改）。
    """
    seen: list[tuple[CircuitState, CircuitState | None]] = []
    breaker = CircuitBreaker(
        failure_threshold=1, state_observer=lambda s, p: seen.append((s, p))
    )

    assert seen == [(CircuitState.CLOSED, None)]
    assert breaker.allow() is True
    breaker.record_failure()
    assert seen[-1] == (CircuitState.OPEN, CircuitState.CLOSED)


def test_breaker_works_without_observer() -> None:
    """不注入观察者也能正常工作：熔断器不假设存在指标系统。"""
    breaker = CircuitBreaker(failure_threshold=1)
    assert breaker.allow() is True
    breaker.record_failure()
    assert breaker.state is CircuitState.OPEN
    assert breaker.allow() is False


def test_release_probe_frees_wedged_half_open() -> None:
    """半开探测被取消后必须释放在途标志，否则熔断器永久卡死。

    真实触发路径：请求处理到一半被取消（服务关闭、调用方断开），
    ``record_success`` 与 ``record_failure`` 都不会被调用。若在途标志不释放，
    此后所有请求都被判为「半开期已有探测在途」而快速失败——下游早已恢复，
    系统却再也不会尝试。
    """
    clock = _FakeClock()
    breaker = CircuitBreaker(
        failure_threshold=1, recovery_timeout_seconds=5.0, clock=clock
    )
    assert breaker.allow() is True
    breaker.record_failure()
    clock.advance(6)

    assert breaker.allow() is True  # 冷却期满，放行一个探测
    assert breaker.allow() is False  # 探测在途，其余快速失败

    breaker.release_probe()  # 探测被取消：既非成功也非失败
    assert breaker.state is CircuitState.HALF_OPEN
    assert breaker.allow() is True  # 下一个请求可以重新探测
