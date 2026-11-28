"""严格 YAML 载入（治理 YAML 单一事实源）的正向 / 负向测试。

为什么需要负向测试
------------------
PyYAML 的 ``safe_load`` 对重复映射键静默取后键。闸门若用 safe_load，
重复键被注入时照样 PASS——等于删掉检查也绿。本测试证明：
1. 严格 loader 能拒绝顶层 / 嵌套 / 序列内重复键，并点名 key 与行号；
2. 合法的 merge key（<<）与不同层同名键不受影响；
3. 闸门 ``contract_check.check_yaml`` 遇到重复键真的报 FAIL 并点名。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from packages.shared.yaml_strict import (  # noqa: E402
    DuplicateYamlKeyError,
    load_yaml,
    load_yaml_file,
)


# --------------------------------------------------------------------------- #
# 正向：合法 YAML 不受影响
# --------------------------------------------------------------------------- #
def test_distinct_keys_at_different_levels_are_allowed() -> None:
    """不同层级的同名键不是重复，应正常解析。"""
    data = load_yaml("root:\n  a: 1\n  nested:\n    a: 2\n", source="ok.yaml")
    assert data == {"root": {"a": 1, "nested": {"a": 2}}}


def test_merge_key_semantics_preserved() -> None:
    """合法的 << merge key 必须保留：显式键覆盖继承键，不算重复。"""
    data = load_yaml(
        "defaults: &d\n  adapter: postgres\n  host: localhost\nservice:\n  <<: *d\n  host: prod\n",
        source="merge.yaml",
    )
    assert data["service"] == {"adapter": "postgres", "host": "prod"}


# --------------------------------------------------------------------------- #
# 负向：重复键必须报红并点名
# --------------------------------------------------------------------------- #
def test_top_level_duplicate_key_is_rejected_with_location() -> None:
    """顶层重复键（正是 revocations 事故形态）必须报红并带 key + 行号 + 来源。"""
    with pytest.raises(DuplicateYamlKeyError) as exc:
        load_yaml("revocations: []\nrevocations: []\n", source="amendments.yaml")
    assert exc.value.key == "revocations"
    assert exc.value.line == 2
    assert exc.value.source == "amendments.yaml"
    assert "revocations" in str(exc.value)


def test_nested_duplicate_key_is_rejected(tmp_path: Path) -> None:
    """嵌套映射内的重复键必须报红。"""
    target = tmp_path / "nested.yaml"
    target.write_text("root:\n  x: 1\n  x: 2\n", encoding="utf-8")
    with pytest.raises(DuplicateYamlKeyError) as exc:
        load_yaml_file(target)
    assert exc.value.key == "x"
    assert exc.value.line == 3


def test_duplicate_key_inside_sequence_mapping_is_rejected(tmp_path: Path) -> None:
    """序列元素映射内的重复键必须报红（契约 actions 列表是这种形态）。"""
    target = tmp_path / "seq.yaml"
    target.write_text("items:\n  - k: 1\n    k: 2\n", encoding="utf-8")
    with pytest.raises(DuplicateYamlKeyError) as exc:
        load_yaml_file(target)
    assert exc.value.key == "k"
    assert exc.value.line == 3


def test_missing_file_is_readable_error(tmp_path: Path) -> None:
    """缺失文件转可读 ValueError，不裸抛 FileNotFoundError。"""
    with pytest.raises(ValueError, match="缺失"):
        load_yaml_file(tmp_path / "nope.yaml")


# --------------------------------------------------------------------------- #
# 闸门层：check_yaml 必须真的 FAIL 并点名
# --------------------------------------------------------------------------- #
def test_gate_check_yaml_fails_and_names_duplicate_key(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """闸门 check_yaml 遇到重复键必须返回 False，并打印 key 与文件名。"""
    import contract_check as cc

    target = tmp_path / "dup.yaml"
    target.write_text("revocations: []\nrevocations: []\n", encoding="utf-8")
    assert cc.check_yaml(target) is False
    out = capsys.readouterr().out
    assert "[FAIL]" in out
    assert "dup.yaml" in out
    assert "revocations" in out


def test_gate_check_yaml_passes_on_valid_contract(tmp_path: Path) -> None:
    """闸门 check_yaml 对合法 YAML 应返回 True（防止把闸门改成永远红）。"""
    import contract_check as cc

    target = tmp_path / "ok.yaml"
    target.write_text("a: 1\nb:\n  c: 2\n", encoding="utf-8")
    assert cc.check_yaml(target) is True
