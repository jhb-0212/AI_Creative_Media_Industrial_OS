"""Action Registry 运行时行为测试（未注册动作 + 解析缓存 + 加载期损坏）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from packages.errors.error_codes import DomainError, ErrorRegistry
from services.identity_access import action_registry
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


# --------------------------------------------------------------------------- #
# 加载期损坏 fail-fast 转结构化 DomainError（T2，复核 R7）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "content, fragment",
    [
        ("", "解析结果不是映射"),  # 空文件
        ("- a\n- b\n", "解析结果不是映射"),  # 顶层是 list
        ("actions: null\n", "必须是列表"),  # actions 非列表
        ("actions: [\"not-a-map\"]\n", "非映射条目"),  # 条目非 dict
    ],
)
def test_corrupt_registry_raises_domain_error(
    content: str, fragment: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """契约损坏（空/非映射/actions 非列表/条目非 dict）→ DomainError(INTERNAL_ERROR)。

    而非裸 FileNotFoundError/ValueError/TypeError 冒泡（R7：docstring 承诺的
    结构化信封必须兑现）。
    """
    action_registry._load_registry.cache_clear()
    tmp = Path("tests/unit/_tmp_action_registry.yaml")
    tmp.write_text(content, encoding="utf-8")
    monkeypatch.setattr(action_registry, "_CONTRACT_PATH", tmp)

    with pytest.raises(DomainError) as exc_info:
        action_registry._load_registry()
    assert exc_info.value.error_code is ErrorRegistry.INTERNAL_ERROR
    assert fragment in exc_info.value.detail

    action_registry._load_registry.cache_clear()
    tmp.unlink(missing_ok=True)

