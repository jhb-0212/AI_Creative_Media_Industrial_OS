"""结构化错误码定义（对齐 Phase 1 验收 D2）。

设计要点（不屎山代码 + 高标准）：
- 错误码稳定、可归类、全局唯一。
- 每个错误码携带：code、http_status、message 模板、retryable 标记。
- 业务错误通过统一异常类型抛出，拒绝在代码里散落魔法字符串。
- 错误响应「无副作用」：抛出即终止，不留下半成品状态（由调用方事务边界保证）。

来源契约：正文 31.6、`contracts/permission/permission_matrix.yaml`。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ErrorCategory(str, Enum):
    """错误大类，用于归类与可观测性统计。"""

    AUTH = "AUTH"
    PERMISSION = "PERMISSION"
    VALIDATION = "VALIDATION"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    STATE = "STATE"
    PHASE = "PHASE"
    RATE_LIMIT = "RATE_LIMIT"
    INTERNAL = "INTERNAL"


@dataclass(frozen=True)
class ErrorCode:
    """不可变错误码定义。"""

    code: str
    category: ErrorCategory
    http_status: int
    message: str
    retryable: bool = False
    # 附加说明字段，用于结构化透出（可选）
    fields: dict[str, Any] = field(default_factory=dict)


class ErrorRegistry:
    """错误码注册表（单一事实源）。"""

    # 权限与鉴权
    AUTH_SCOPE_DENIED = ErrorCode(
        code="AUTH_SCOPE_DENIED",
        category=ErrorCategory.PERMISSION,
        http_status=403,
        message="调用主体缺少执行该动作所需的权限范围",
    )
    AUTH_UNAUTHENTICATED = ErrorCode(
        code="AUTH_UNAUTHENTICATED",
        category=ErrorCategory.AUTH,
        http_status=401,
        message="未认证或凭证无效",
    )
    AUTH_TOKEN_EXPIRED = ErrorCode(
        code="AUTH_TOKEN_EXPIRED",
        category=ErrorCategory.AUTH,
        http_status=401,
        message="CapabilityToken 已过期",
    )

    # 阶段未启用
    PHASE_NOT_ENABLED = ErrorCode(
        code="PHASE_NOT_ENABLED",
        category=ErrorCategory.PHASE,
        http_status=409,
        message="该能力在当前平台阶段未启用",
    )

    # 校验与状态
    VALIDATION_ERROR = ErrorCode(
        code="VALIDATION_ERROR",
        category=ErrorCategory.VALIDATION,
        http_status=422,
        message="请求校验失败",
    )
    STATE_TRANSITION_DENIED = ErrorCode(
        code="STATE_TRANSITION_DENIED",
        category=ErrorCategory.STATE,
        http_status=409,
        message="非法状态转移，已拒绝",
    )
    IDEMPOTENCY_CONFLICT = ErrorCode(
        code="IDEMPOTENCY_CONFLICT",
        category=ErrorCategory.CONFLICT,
        http_status=409,
        message="重复请求冲突，返回原结果",
    )
    NOT_FOUND = ErrorCode(
        code="NOT_FOUND",
        category=ErrorCategory.NOT_FOUND,
        http_status=404,
        message="目标对象不存在",
    )

    # 内部错误
    INTERNAL_ERROR = ErrorCode(
        code="INTERNAL_ERROR",
        category=ErrorCategory.INTERNAL,
        http_status=500,
        message="内部错误",
        retryable=True,
    )


class DomainError(Exception):
    """领域业务异常的基类，携带结构化错误码。"""

    def __init__(
        self,
        error_code: ErrorCode,
        *,
        detail: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        self.error_code = error_code
        self.detail = detail or error_code.message
        self.context = context or {}
        super().__init__(self.detail)


def to_response_payload(exc: DomainError) -> dict[str, Any]:
    """将领域异常转换为稳定的结构化响应体。"""
    return {
        "code": exc.error_code.code,
        "category": exc.error_code.category.value,
        "message": exc.detail,
        "retryable": exc.error_code.retryable,
        "context": exc.context,
    }
