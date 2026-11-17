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

# 阶段门禁拒绝（F1）。标签只放 action_key：取值来自 Action Registry 的有限集
# （当前 44 条 PHASE_GATED），基数有界。存在的意义是让"有人在敲未开放能力"
# 在指标里可见——只靠 404 计数无法把这种尝试与拼错路径区分开。
PHASE_GATE_REJECTIONS_TOTAL = Counter(
    "phase_gate_rejections_total",
    "阶段门禁拒绝次数（action_key=被拒绝的契约动作）",
    ["action_key"],
)

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
