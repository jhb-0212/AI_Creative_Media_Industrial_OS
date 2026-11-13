"""errors 包：结构化错误码与领域异常。"""

from packages.errors.error_codes import (
    DomainError,
    ErrorCategory,
    ErrorCode,
    ErrorRegistry,
    to_response_payload,
)

__all__ = [
    "DomainError",
    "ErrorCategory",
    "ErrorCode",
    "ErrorRegistry",
    "to_response_payload",
]
