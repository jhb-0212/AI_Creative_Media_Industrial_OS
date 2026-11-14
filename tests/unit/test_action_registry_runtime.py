"""Action Registry 运行时行为测试（未注册动作 + 解析缓存）。"""

from __future__ import annotations

import pytest

from packages.errors.error_codes import DomainError, ErrorRegistry
from services.identity_access.action_registry import (
    allowed_roles,
    parse_allowed_principals,
)


def test_unknown_action_raises_domain_error() -> None:
    """未注册 action_key 不再抛裸 ValueError（会绕过错误信封）。"""
    with pytest.raises(DomainError) as exc_info:
        allowed_roles("NO.SUCH.ACTION")

    assert exc_info.value.error_code is ErrorRegistry.INTERNAL_ERROR


def test_parse_results_are_cached() -> None:
    """同一 principals 文本重复解析返回同一对象（每请求不再重跑正则）。"""
    text = "导演 / 制片人（授权范围内）/ 编剧"
    first = parse_allowed_principals(text)
    second = parse_allowed_principals(text)

    assert first == second
    assert first is second
