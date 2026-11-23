"""类型检查覆盖面必须派生，且「新增一层」要自动被纳入。

这条测试守的是「清单漏项」这个病：写死的目录清单在新增一层时不会报错，
它只是漏掉，而漏掉的层没有类型保护也没有信号。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import mypy_targets as module


def test_adapters_is_covered_because_it_has_code() -> None:
    """本轮的实际触发点：adapters 落地真实实现后必须进入类型检查。"""
    assert "adapters" in module.mypy_targets()


def test_empty_layer_is_skipped() -> None:
    """只有 .gitkeep 的层不纳入：否则 mypy 报 "no .py files" 看起来像类型错误。"""
    targets = module.mypy_targets()
    assert "agents" not in targets


def test_targets_are_subset_of_declared_layers() -> None:
    from scripts.architecture_check import LAYERS

    assert set(module.mypy_targets()) <= set(LAYERS)


def test_new_layer_with_code_is_picked_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """在派生根下新建一个有 .py 的层，必须自动出现——这是派生的意义所在。"""
    layers = ("packages", "agents", "brand_new_layer")
    for layer in layers:
        (tmp_path / layer).mkdir()
    (tmp_path / "packages" / "a.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "brand_new_layer" / "b.py").write_text("y = 2\n", encoding="utf-8")

    # LAYERS 由模块顶层 import 进来，monkeypatch 必须作用于该名字本身；
    # _REPO_ROOT 同理，否则派生仍指向真实仓库。
    monkeypatch.setattr(module, "_REPO_ROOT", tmp_path)
    monkeypatch.setattr(module, "LAYERS", layers)

    targets = module.mypy_targets()
    assert "packages" in targets
    assert "brand_new_layer" in targets
    assert "agents" not in targets
