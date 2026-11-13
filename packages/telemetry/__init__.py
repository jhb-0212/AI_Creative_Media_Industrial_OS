"""telemetry 包：OpenTelemetry 分布式追踪初始化。"""

from packages.telemetry.tracing import get_current_trace_id, setup_telemetry

__all__ = ["get_current_trace_id", "setup_telemetry"]
