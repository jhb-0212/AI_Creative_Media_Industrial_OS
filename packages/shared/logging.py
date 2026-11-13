"""结构化日志（structlog）初始化。

要求（红线 6「密钥不落日志」 + 可观测性 A3）：
- 统一 JSON 输出，便于采集与检索；同时接管标准库 logging，让 uvicorn 等第三方
  日志也走同一 JSON 格式，避免「一个普通格式、一个 JSON」的割裂。
- 通过 processors 裁剪/脱敏敏感字段（密钥不落日志）。
- 每条日志自动带 trace_id（contextvars 注入，供 OpenTelemetry 链路贯通）。
"""

from __future__ import annotations

import contextvars
import logging
import sys

import structlog

# 当前请求/任务链路的 trace 标识，由中间件或 OTel 写入。
trace_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "trace_id", default=None
)


def _redact_sensitive(_: object, __: str, event_dict: dict) -> dict:
    """脱敏处理：剔除敏感键，避免密钥落日志。"""
    sensitive_keys = {
        "password",
        "secret",
        "api_key",
        "access_key",
        "token",
        "authorization",
        "credential",
    }
    for key in list(event_dict.keys()):
        if any(s in key.lower() for s in sensitive_keys):
            event_dict[key] = "***REDACTED***"
    return event_dict


def _inject_trace_id(_: object, __: str, event_dict: dict) -> dict:
    """注入当前 trace_id（若无则标记为 unknown）。"""
    trace_id = trace_id_var.get()
    event_dict["trace_id"] = trace_id or "unknown"
    return event_dict


def _shared_processors() -> list:
    """structlog 与 stdlib 共用的处理器链。"""
    return [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        _inject_trace_id,
        _redact_sensitive,
    ]


def setup_logging(level: str = "INFO") -> None:
    """配置 structlog 为 JSON 输出，并将 stdlib logging 桥接到同一格式。

    桥接后，uvicorn / uvicorn.access / uvicorn.error 的输出也会以 JSON 呈现，
    消除「第三方库默认格式」与「应用 JSON」并存的问题。
    """
    log_level = getattr(logging, level.upper(), logging.INFO)

    # 1) 重置根 logger，避免重复 handler。
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(log_level)

    # 2) 构造一个输出 JSON 的 handler，供 stdlib logger 使用。
    json_handler = logging.StreamHandler(sys.stdout)
    json_handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            processor=structlog.processors.JSONRenderer(),
            foreign_pre_chain=_shared_processors(),
        )
    )
    root.addHandler(json_handler)

    # 3) uvicorn.error 走根 handler（JSON 输出）；uvicorn.access 关闭，
    #    由应用中间件统一记录请求日志，避免重复。
    uvicorn_error = logging.getLogger("uvicorn.error")
    uvicorn_error.handlers = []
    uvicorn_error.propagate = True

    uvicorn_access = logging.getLogger("uvicorn.access")
    uvicorn_access.handlers = []
    uvicorn_access.propagate = False
    uvicorn_access.disabled = True

    uvicorn_root = logging.getLogger("uvicorn")
    uvicorn_root.handlers = []
    uvicorn_root.propagate = True

    # 4) 配置 structlog（应用侧）。
    structlog.configure(
        processors=_shared_processors() + [structlog.processors.JSONRenderer()],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        logger_factory=structlog.PrintLoggerFactory(sys.stdout),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """获取绑定 logger。"""
    return structlog.get_logger(name)


def set_trace_id(trace_id: str | None) -> None:
    """写入当前上下文 trace_id（由请求中间件/OTel 调用）。"""
    trace_id_var.set(trace_id)
