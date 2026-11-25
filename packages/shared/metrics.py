"""Prometheus 指标（与 OpenTelemetry 追踪并行，不替代追踪）。

约定：
- 标签只放低基数维度（method、status），**绝不放 path**：路径里含实体 ID，
  会把指标基数打爆。路径维度由结构化日志与追踪承担。
- 指标只在进程内聚合；多副本由 Prometheus 各自抓取后聚合，不在应用层做汇总。
"""

from __future__ import annotations

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from starlette.responses import Response

HTTP_REQUESTS_TOTAL = Counter(
    "http_requests_total",
    "HTTP 请求总数（按方法与状态码）",
    ["method", "status_code"],
)

HTTP_REQUEST_DURATION_SECONDS = Histogram(
    "http_request_duration_seconds",
    "HTTP 请求耗时（秒）",
    ["method"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)

RATE_LIMIT_DECISIONS_TOTAL = Counter(
    "rate_limit_decisions_total",
    "限流判定次数（decision=allowed/denied）",
    ["decision"],
)

RATE_LIMIT_BACKEND_ERRORS = Counter(
    "rate_limit_backend_errors_total",
    "限流后端（Redis）故障次数；故障时按 fail-open 放行",
)

# 限流后端熔断状态。与 webhook 端点熔断分开命名：两者会同时存在，
# 共用一个指标名会让「哪个组件在熔断」不可分辨。
RATE_LIMIT_CIRCUIT_STATE = Gauge(
    "rate_limit_circuit_state",
    "限流后端熔断器当前状态（1=当前态；state=closed/open/half_open）",
    ["state"],
)


def observe_circuit_state(gauge: Gauge, state: str, previous: str | None) -> None:
    """熔断状态观察者的共用实现：清旧置新。

    作为 ``CircuitBreaker(state_observer=...)`` 的适配器：熔断器只负责在迁移时
    回调 ``(new, previous)``，具体记到哪个 Gauge 由使用方决定。
    ``previous`` 为空表示首次发布（构造期），只需置 1。
    """
    if previous is not None and previous != state:
        gauge.labels(state=previous).set(0)
    gauge.labels(state=state).set(1)

# Outbox relay（R06 消费端）。标签只放 event_type 与 result：event_type 由契约
# 枚举约束（有限集），result 是 delivered/unrouted/failed 三值，基数可控。
OUTBOX_RELAY_EVENTS_TOTAL = Counter(
    "outbox_relay_events_total",
    "Outbox 事件处理结果（result=delivered/unrouted/failed）",
    ["event_type", "result"],
)

# 积压用无标签 Gauge 而不是按租户打标签：租户数会增长，按租户做标签迟早出事。
OUTBOX_RELAY_BACKLOG = Gauge(
    "outbox_relay_backlog",
    "待投递事件总数（最近一轮扫描采样，只计可领取，不含死信与退避）",
)

# 死信单独观测：与 backlog 分离，避免「已放弃的死信」虚高积压报警（复核 R2）。
OUTBOX_RELAY_DEAD = Gauge(
    "outbox_relay_dead",
    "死信事件总数（最近一轮扫描采样，投递已放弃、待人工重放）",
)

# 人工重放计数。与 OUTBOX_RELAY_DEAD 分工明确：Gauge 回答「现在积压多少死信」，
# Counter 回答「处置动作发生了多少次」——只看 Gauge 时，「一直有 5 条」与
# 「重放了 100 条又新进 100 条」在曲线上无法区分。
#
# 口径（台账 D69 残余风险第 2 条要求先定口径）：
# - 只计**成功**重放：拒绝路径（未死信 / 已投递 / 跨租户 / 并发）是「有人在做
#   不该做的事」或「看错了状态」，它的证据在结构化日志与 4xx 指标里，不该混进
#   「重放次数」——否则「这个月重放了多少条」永远算不对。
# - 唯一的标签是 event_type：它由契约枚举约束（有限集），且排障时最有用的问句
#   「哪类事件在反复进死信」刚好按它聚合。不打 operator（人员维度会随时间单调
#   增长，且不该用于阈值告警）。
OUTBOX_DEAD_LETTER_REPLAYS_TOTAL = Counter(
    "outbox_dead_letter_replays_total",
    "死信人工重放成功次数（按事件类型）",
    ["event_type"],
)

OUTBOX_RELAY_RUN_ERRORS = Counter(
    "outbox_relay_run_errors_total",
    "Relay 单轮失败次数（stage=enumerate/tenant）",
    ["stage"],
)

# Webhook 端点熔断状态（复核 R9）。三值 Gauge：当前状态为 1、其余为 0，
# 不打 endpoint 标签——webhook 是单值配置，endpoint 不是高基数维度。
OUTBOX_WEBHOOK_CIRCUIT_STATE = Gauge(
    "outbox_webhook_circuit_state",
    "Webhook 端点熔断器当前状态（1=当前态；state=closed/open/half_open）",
    ["state"],
)


def metrics_response() -> Response:
    """渲染 Prometheus 文本格式（/metrics 端点直接返回）。"""
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
