"""packages/telemetry 的单元测试。"""

from __future__ import annotations

from packages.telemetry.tracing import get_current_trace_id


def test_get_current_trace_id_returns_none_without_span() -> None:
    """无活跃 span 时返回 None，不抛异常（优雅降级）。"""
    assert get_current_trace_id() is None
