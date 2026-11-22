"""packages/errors 结构化错误码的单元测试（对齐 Phase 1 验收 D2）。"""

from __future__ import annotations

from packages.errors.error_codes import DomainError, ErrorRegistry, to_response_payload


def test_error_code_is_stable_and_categorizable() -> None:
    code = ErrorRegistry.AUTH_SCOPE_DENIED
    assert code.code == "AUTH_SCOPE_DENIED"
    assert code.category.value == "PERMISSION"
    assert code.http_status == 403


def test_domain_error_to_response_payload() -> None:
    exc = DomainError(ErrorRegistry.PHASE_NOT_ENABLED, detail="该能力未启用")
    payload = to_response_payload(exc)
    assert payload["error_code"] == "PHASE_NOT_ENABLED"
    assert payload["category"] == "PHASE"
    assert payload["message"] == "该能力未启用"
    assert payload["retryable"] is False


def test_error_registry_codes_are_unique() -> None:
    codes = [
        getattr(ErrorRegistry, attr).code
        for attr in dir(ErrorRegistry)
        if not attr.startswith("_")
    ]
    assert len(codes) == len(set(codes)), "错误码必须全局唯一"


def test_internal_error_is_retryable() -> None:
    assert ErrorRegistry.INTERNAL_ERROR.retryable is True
