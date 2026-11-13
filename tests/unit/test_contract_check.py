"""契约一致性校验脚本测试。

覆盖：
1. parse_state_chain 解析器：连续箭头、斜杠多源、中英文分隔符、空串。
2. check_project_state_machine 正向：当前契约与实现一致，应通过。
3. 负向：临时制造不一致（契约多/少转移），应被检出。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# 将 scripts 加入导入路径（scripts 不是包，直接按模块加载）。
SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import contract_check as cc  # noqa: E402


# --------------------------------------------------------------------------- #
# parse_state_chain 解析器
# --------------------------------------------------------------------------- #
def test_parse_simple_chain() -> None:
    assert cc.parse_state_chain("DRAFT→ACTIVE") == {("DRAFT", "ACTIVE")}


def test_parse_consecutive_arrows() -> None:
    assert cc.parse_state_chain("DRAFT→ACTIVE→ON_HOLD") == {
        ("DRAFT", "ACTIVE"),
        ("ACTIVE", "ON_HOLD"),
    }


def test_parse_slash_multi_source() -> None:
    assert cc.parse_state_chain("DRAFT/ON_HOLD/COMPLETED→ARCHIVED") == {
        ("DRAFT", "ARCHIVED"),
        ("ON_HOLD", "ARCHIVED"),
        ("COMPLETED", "ARCHIVED"),
    }


def test_parse_full_contract_chain() -> None:
    text = (
        "DRAFT→ACTIVE→ON_HOLD；ACTIVE→COMPLETED；"
        "ON_HOLD→ACTIVE；DRAFT/ON_HOLD/COMPLETED→ARCHIVED"
    )
    expected = {
        ("DRAFT", "ACTIVE"),
        ("ACTIVE", "ON_HOLD"),
        ("ACTIVE", "COMPLETED"),
        ("ON_HOLD", "ACTIVE"),
        ("DRAFT", "ARCHIVED"),
        ("ON_HOLD", "ARCHIVED"),
        ("COMPLETED", "ARCHIVED"),
    }
    assert cc.parse_state_chain(text) == expected


def test_parse_ascii_arrows_and_semicolons() -> None:
    assert cc.parse_state_chain("A->B; B/C->D") == {
        ("A", "B"),
        ("B", "D"),
        ("C", "D"),
    }


def test_parse_empty_returns_empty() -> None:
    assert cc.parse_state_chain("") == set()
    assert cc.parse_state_chain("   ") == set()


# --------------------------------------------------------------------------- #
# check_project_state_machine 正向 / 负向
# --------------------------------------------------------------------------- #
def test_check_project_state_machine_passes_on_consistent() -> None:
    """当前契约与实现一致，应返回 True。"""
    assert cc.check_project_state_machine() is True


def test_check_project_state_machine_detects_missing_in_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """契约缺少实现已有转移时，应返回 False（负向）。"""
    # 契约缺少 (ACTIVE, ON_HOLD) 这条实现已有的转移
    fake_transitions = {
        ("DRAFT", "ACTIVE"),
        ("ACTIVE", "COMPLETED"),
        ("ON_HOLD", "ACTIVE"),
        ("DRAFT", "ARCHIVED"),
        ("ON_HOLD", "ARCHIVED"),
        ("COMPLETED", "ARCHIVED"),
    }
    monkeypatch.setattr(cc, "parse_state_chain", lambda text: fake_transitions)
    assert cc.check_project_state_machine() is False


def test_check_project_state_machine_detects_extra_in_impl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """契约存在实现未登记的转移时，应返回 False（负向）。"""
    # 契约多一条实现未登记的转移 (COMPLETED, ACTIVE)
    fake_transitions = {
        ("DRAFT", "ACTIVE"),
        ("ACTIVE", "ON_HOLD"),
        ("ACTIVE", "COMPLETED"),
        ("ON_HOLD", "ACTIVE"),
        ("DRAFT", "ARCHIVED"),
        ("ON_HOLD", "ARCHIVED"),
        ("COMPLETED", "ARCHIVED"),
        ("COMPLETED", "ACTIVE"),  # 实现未登记，应被检出
    }
    monkeypatch.setattr(cc, "parse_state_chain", lambda text: fake_transitions)
    assert cc.check_project_state_machine() is False
