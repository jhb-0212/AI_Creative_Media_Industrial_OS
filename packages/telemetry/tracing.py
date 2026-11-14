"""OpenTelemetry 初始化（可观测性 A3：trace_id/job_id/project_id 贯通）。

设计要点：
- 优雅降级：未配置 OTLP endpoint 时，不启用 exporter，仅保留进程内 trace 上下文，
  不影响服务启动（避免「可观测性反而拖垮可用性」）。
- 与 packages.shared.logging 打通：日志的 trace_id 直接取自当前 OTel span，
  实现「日志-链路」一体化，而非两套并行的 trace_id。
- 提供 resource 统一 service.name，便于跨服务聚合。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ALWAYS_ON

from packages.shared.config import get_settings

if TYPE_CHECKING:
    from fastapi import FastAPI

# 全局单例，避免重复初始化。
_initialized = False
# 当前 TracerProvider：关闭时需要 force_flush + shutdown，否则批量缓冲里的
# span 会在进程退出时丢失。
_provider: TracerProvider | None = None


def setup_telemetry(app: FastAPI) -> None:
    """初始化 tracer 并装配 FastAPI 自动埋点。

    Args:
        app: FastAPI 应用实例。
    """
    global _initialized, _provider
    if _initialized:
        return

    settings = get_settings()
    resource = Resource.create(
        {"service.name": settings.otel_service_name, "service.version": settings.app_version}
    )

    provider = TracerProvider(resource=resource, sampler=ALWAYS_ON)

    endpoint = settings.otel_exporter_otlp_endpoint
    if endpoint:
        # 已配置 OTLP 端点，启用 gRPC exporter（批量导出，异步不阻塞请求）。
        # 安全：默认启用 TLS（insecure=False），链路数据加密传输；
        # 仅当显式配置 insecure 时才走明文（限内网/本机 Collector 联调）。
        exporter = OTLPSpanExporter(
            endpoint=endpoint,
            insecure=settings.otel_exporter_otlp_insecure,
        )
        provider.add_span_processor(BatchSpanProcessor(exporter))

    trace.set_tracer_provider(provider)

    # 自动埋点 FastAPI（HTTP 请求/响应 span），并捕获请求异常。
    FastAPIInstrumentor.instrument_app(app)

    _provider = provider
    _initialized = True


def shutdown_telemetry() -> None:
    """关闭 tracer provider：先 flush 缓冲 span，再 shutdown，最后允许重新初始化。

    在应用 lifespan 的关闭阶段调用，保证进程退出前不丢链路数据；同时重置
    全局标记，使测试或多次启动场景不会因 ``_initialized`` 卡死。
    """
    global _initialized, _provider
    if _provider is not None:
        _provider.force_flush()
        _provider.shutdown()
        _provider = None
    _initialized = False


def get_current_trace_id() -> str | None:
    """获取当前 span 的 trace_id（十六进制），无 span 时返回 None。

    供 logging 层注入，实现日志与链路 trace_id 一致。
    """
    span = trace.get_current_span()
    if span is None or not span.is_recording():
        return None
    ctx = span.get_span_context()
    if not ctx.is_valid:
        return None
    return format(ctx.trace_id, "032x")
