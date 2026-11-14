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


# --------------------------------------------------------------------------- #
# 扩展校验：Action/OpenAPI/Permission/Schema
# --------------------------------------------------------------------------- #
def test_normalize_action_key() -> None:
    assert cc._normalize_action_key("PROJECT.CREATE") == "project.create"
    assert cc._normalize_action_key("  Shot.Draft.Create ") == "shot.draft.create"


def test_check_action_openapi_consistency_passes() -> None:
    assert cc.check_action_openapi_consistency() is True


def test_check_permission_matrix_passes() -> None:
    assert cc.check_permission_matrix() is True


def test_check_schema_objects_passes() -> None:
    assert cc.check_schema_objects() is True


def test_action_openapi_mismatch_semantics(monkeypatch, tmp_path) -> None:
    """真实调用被测函数：删掉一个 action 后必须识别出不一致（返回 False）。

    不复制校验逻辑，只构造契约副本，保证用例能随被测实现一起变红。
    """
    import shutil

    import yaml

    contracts = tmp_path / "contracts"
    shutil.copytree(cc.CONTRACTS, contracts)

    registry_path = contracts / "action_registry" / "action_registry.yaml"
    data = yaml.safe_load(registry_path.read_text(encoding="utf-8"))
    data["actions"] = [
        action
        for action in data["actions"]
        if action.get("action_key") != "SCRIPT.IMPORT"
    ]
    data["total_actions"] = len(data["actions"])
    registry_path.write_text(
        yaml.safe_dump(data, allow_unicode=True), encoding="utf-8"
    )

    monkeypatch.setattr(cc, "CONTRACTS", contracts)

    assert cc.check_action_openapi_consistency() is False


# --------------------------------------------------------------------------- #
# 运行时 ↔ 契约覆盖（台账不能撒谎）
# --------------------------------------------------------------------------- #
def test_runtime_contract_coverage_passes() -> None:
    """当前台账、契约与真实路由三方一致。"""
    assert cc.check_runtime_contract_coverage() is True


def test_runtime_coverage_detects_phantom_implementation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """台账声称已实现、运行时却没有该端点 → 必须变红（声明不能撒谎）。"""
    real = cc._runtime_endpoints()
    monkeypatch.setattr(
        cc,
        "_runtime_endpoints",
        lambda: real - {"POST /scripts/import"},
    )
    assert cc.check_runtime_contract_coverage() is False


def test_runtime_coverage_detects_unregistered_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """运行时多出未登记端点 → 必须变红（新端点不能绕过契约）。"""
    real = cc._runtime_endpoints()
    monkeypatch.setattr(
        cc,
        "_runtime_endpoints",
        lambda: real | {"POST /shadow/undocumented"},
    )
    assert cc.check_runtime_contract_coverage() is False


def test_runtime_coverage_detects_unlogged_active_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ACTIVE action 既没实现也没登记原因 → 必须变红。"""
    original = cc._active_action_keys
    monkeypatch.setattr(
        cc, "_active_action_keys", lambda: original() | {"SHOT.GHOST.ACTION"}
    )
    assert cc.check_runtime_contract_coverage() is False
