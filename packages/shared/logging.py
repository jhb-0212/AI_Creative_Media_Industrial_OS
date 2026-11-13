"""结构化日志（structlog）初始化。

要求（红线 6「密钥不落日志」）：
- 统一 JSON 输出，便于采集与检索。
- 通过 structlog 的 processors 裁剪/脱敏敏感字段。
- 每条日志自动带上 trace_id / job_id / project_id（可观测性 A3）。
"""

from __future__ import annotations

import logging
import sys

import structlog


def _redact_sensitive(_: object, __: str, event_dict: dict) -> dict:
    """脱敏处理：剔除明显的敏感键，避免密钥落日志。"""
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


def setup_logging(level: str = "INFO") -> None:
    """配置 structlog 为 JSON 输出，绑定基础上下文。"""
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            _redact_sensitive,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        logger_factory=structlog.PrintLoggerFactory(sys.stdout),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """获取绑定 logger。"""
    return structlog.get_logger(name)
