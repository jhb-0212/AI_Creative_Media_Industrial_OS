"""shared 包：横切能力（配置、结构化日志等）。"""

from packages.shared.config import Settings, get_settings
from packages.shared.logging import get_logger, set_trace_id, setup_logging

__all__ = ["Settings", "get_settings", "get_logger", "set_trace_id", "setup_logging"]
