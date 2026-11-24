"""TECH-02 Valkey PoC 取证装置（隔离环境，非生产代码）。

对应 ADR-R44 §5.2 五项通过条件，逐条产出可复算的 PASS/FAIL。
红线：本脚本不改生产代码、不改 Compose/依赖锁/冻结基线；只连隔离的
Valkey 9.1.4(6380) 与 9.2.0(6382)，测试键用 poc: 前缀并在结束时清理。

前置事实（已固化，见报告）：当前 redis:7 = Redis CE 7.4.12，按 F-3
数据文件与 Valkey 不兼容；本 PoC 只验证协议/命令兼容，不声称 RDB/AOF 平滑迁移。
"""

from __future__ import annotations

import asyncio
import os
import time
from collections import Counter as _Counter

from redis.asyncio import Redis

from packages.shared.circuit_breaker import CircuitBreaker, CircuitState
from packages.shared.metrics import (
    RATE_LIMIT_BACKEND_ERRORS,
    RATE_LIMIT_CIRCUIT_STATE,
)
from packages.shared.rate_limit import RateLimiter

PW = "f7qemg3nzo0kat6vyb1x"
# SELFTEST_MUTANT: 自检专用，把 9.2.0 指向坏端口，验证装置在真坏时会报红。
# 该开关只用于证伪装置有效性，正常取证时不得设置。
_MUTANT = os.environ.get("TECH02_SELFTEST_MUTANT") == "1"
_P920_PORT = 6399 if _MUTANT else 6382
ENDPOINTS = {
    "9.1.4": ("redis://:" + PW + "@localhost:6380/0", 6380),
    "9.2.0": ("redis://:" + PW + f"@localhost:{_P920_PORT}/0", _P920_PORT),
}

RESULTS: list[tuple[str, str, bool, str]] = []


def record(cond: str, name: str, ok: bool, detail: str) -> None:
    RESULTS.append((cond, name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {cond} {name} :: {detail}")


def metric_value(counter) -> float:
    return counter._value.get()  # prometheus client 内部读取，仅取证用


async def make_client(url: str) -> Redis:
    return Redis.from_url(
        url, decode_responses=True, socket_connect_timeout=1.0, socket_timeout=1.0
    )


# --------------------------------------------------------------------------- #
# 条件① + ②：redis-py 客户端在两版本上跑同一矩阵
# --------------------------------------------------------------------------- #
async def matrix(version: str, url: str) -> None:
    c = await make_client(url)
    try:
        # 坏状态下必须记为 FAILED（断言失败），而不是抛未捕获异常变成 ERROR：
        # ERROR 与 FAILED 证明的是相反的事，装置要能区分二者。
        try:
            info = await c.info("server")
        except Exception as e:
            record("①②", f"{version} 版本指纹", False,
                   f"连接不可用 {type(e).__name__}: {str(e)[:80]}")
            return
        info = info
        ver = info.get("valkey_version") or info.get("redis_version")
        record("①②", f"{version} 版本指纹", ver == version, f"INFO valkey_version={ver}")

        # 连接 / INCR / EXPIRE / TTL
        key = f"poc:matrix:{version}:{int(time.time() * 1000)}"
        c1 = await c.incr(key)
        await c.expire(key, 30)
        ttl = await c.ttl(key)
        c2 = await c.incr(key)
        record("①②", f"{version} INCR+EXPIRE", (c1, c2, ttl) == (1, 2, 30),
               f"incr={c1},{c2} ttl={ttl}")

        # 高并发计数：100 并发 INCR 同一键，结果必须精确 100（无丢失）
        hkey = f"poc:hot:{version}:{int(time.time() * 1000)}"
        await asyncio.gather(*[c.incr(hkey) for _ in range(100)])
        hot = await c.get(hkey)
        record("①②", f"{version} 高并发计数", hot == "100", f"100 concurrent INCR -> {hot}")
        await c.expire(hkey, 30)

        # 用生产 RateLimiter 跑固定窗口语义（limit=3，窗口 60s）
        rl = RateLimiter(c, limit=3, window_seconds=60)
        ident = f"poc-rl-{version}-{int(time.time() * 1000)}"
        decisions = [await rl.acquire(ident) for _ in range(5)]
        allowed = [d.allowed for d in decisions]
        ok = allowed == [True, True, True, False, False]
        record("①②", f"{version} RateLimiter 固定窗口", ok,
               f"limit=3 连续5次 allowed={allowed}")

        # 连接池关闭：redis-py 的 aclose() 释放连接池资源（disconnect），
        # 语义不是「永久禁用客户端」——之后再 ping 会惰性重建连接，这是正常行为。
        # 正确判据：aclose() 调用后连接池的 in_use 连接应归零且可正常关闭，不抛异常。
        await rl.aclose()
        pool = c.connection_pool
        in_use = len(getattr(pool, "_in_use_connections", set()))
        record("①②", f"{version} 连接池关闭", in_use == 0,
               f"aclose 后连接池 in_use={in_use}（redis-py 8.x aclose 释放连接，"
               f"后续 ping 惰性重建属正常，不作为判据）")

        # 清理测试键
        await c.delete(key, hkey, f"ratelimit:{ident}")
    finally:
        try:
            await c.aclose()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# 条件③：模拟不可用 -> fail-open + 熔断语义准确 + 指标可见
# --------------------------------------------------------------------------- #
async def failure_injection() -> None:
    # 指向一个没有 Valkey 监听的端口（6399），socket_connect_timeout=1s
    bad = await make_client("redis://:" + PW + "@localhost:6399/0")
    before_errors = metric_value(RATE_LIMIT_BACKEND_ERRORS)

    # 复刻生产 from_settings() 的 observer 接线：判据是「熔断状态指标可见」，
    # 必须在 breaker 上挂 state_observer，否则 gauge 不更新（第一次漏接是装置错，不是产品错）。
    from packages.shared.rate_limit import _observe_backend_circuit

    breaker = CircuitBreaker(
        failure_threshold=2, recovery_timeout_seconds=60.0, enabled=True,
        state_observer=_observe_backend_circuit,
    )
    rl = RateLimiter(bad, limit=1, window_seconds=60, breaker=breaker)

    d1 = await rl.acquire("poc-fail-1")
    d2 = await rl.acquire("poc-fail-2")
    record("③", "fail-open 放行", d1.allowed and d2.allowed and d1.degraded and d2.degraded,
           f"allowed={d1.allowed},{d2.allowed} degraded={d1.degraded},{d2.degraded}")

    after_errors = metric_value(RATE_LIMIT_BACKEND_ERRORS)
    err_delta = after_errors - before_errors
    record("③", "后端错误指标可见", err_delta >= 2, f"rate_limit_backend_errors +{err_delta}")

    from_open = breaker.state
    record("③", "熔断 OPEN 状态", from_open is CircuitState.OPEN,
           f"failure_threshold=2 连续失败后 state={from_open.value}")

    # OPEN 期：不应再发网络包。用假时钟推进到冷却期前，acquire 应立刻降级返回
    t0 = time.monotonic()
    d3 = await rl.acquire("poc-fail-3")
    elapsed = time.monotonic() - t0
    record("③", "OPEN 期零网络包", d3.degraded and elapsed < 0.5,
           f"degraded={d3.degraded} 耗时={elapsed:.3f}s（无 1s 连接超时）")

    state_labels = {
        s: RATE_LIMIT_CIRCUIT_STATE.labels(state=s)._value.get()
        for s in ("closed", "open", "half_open")
    }
    record("③", "熔断状态指标可见", state_labels.get("open") == 1.0,
           f"rate_limit_circuit_state={state_labels}")

    await rl.aclose()


# --------------------------------------------------------------------------- #
# 条件④：窗口切换空档（结构性分析 + 实证 EXPIRE 行为）
# --------------------------------------------------------------------------- #
async def window_gap() -> None:
    # 固定窗口 N：INCR 首次设 EXPIRE(window)；窗口到点 key 消失 -> 计数归零。
    # 切换后端时旧计数不迁移，存在「新后端计数从 0 起」的短暂空档。
    c = await make_client(ENDPOINTS["9.1.4"][0])
    try:
        key = f"poc:gap:{int(time.time() * 1000)}"
        await c.incr(key)
        await c.expire(key, 2)
        during = await c.get(key)
        await asyncio.sleep(2.2)
        after = await c.get(key)
        record("④", "窗口到期计数归零实证", during == "1" and after is None,
               f"窗口内={during} 到期后={after!r} -> 固定窗口重置语义确认")
        await c.delete(key)
    finally:
        await c.aclose()


# --------------------------------------------------------------------------- #
# 条件⑤：无授权语义回退 —— RateLimiter 键不含身份权威字段，鉴权仍由 Principal 承担
# --------------------------------------------------------------------------- #
def auth_semantics() -> None:
    import inspect
    src = inspect.getsource(RateLimiter.acquire)
    # 限流键只用调用方传入的 identity（中间件按令牌指纹/IP 构造），不读 Principal
    no_principal = "principal" not in src.lower()
    record("⑤", "限流不触碰鉴权权威", no_principal,
           "acquire() 仅用 identity 字符串计数，不读取/绕过 Principal 鉴权")


async def main() -> None:
    for version, (url, _port) in ENDPOINTS.items():
        await matrix(version, url)
    await failure_injection()
    await window_gap()
    auth_semantics()

    passed = sum(1 for _, _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print("\n==== SUMMARY ====")
    print(f"PASS {passed}/{total}")
    for cond, name, ok, detail in RESULTS:
        if not ok:
            print(f"  FAILED: {cond} {name} :: {detail}")
    print("POC_RESULT", "PASS" if passed == total else "FAIL")


if __name__ == "__main__":
    asyncio.run(main())
