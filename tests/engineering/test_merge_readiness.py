"""合并前自检闸门（scripts/merge_readiness.py）的测试。

两组，缺一不可（沿用 architecture_check 测试的既有教训）：
1. **仓库现状**：真仓库必须 0 发现——防止闸门上线即假绿或长期红着没人管；
2. **闸门自身**：在临时目录构造坏状态，逐条证明它能 FAIL 并**点名**——
   闸门没有负向测试，就等于「删两行条目也能绿」。

特别针对 2026-11-21 双线整合暴露的三类沉默缺陷各给一个负向用例：
字典重复键（_RESOLVE_SQL 事故）、同名顶层定义、新代码目录免疫。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import merge_readiness as mr  # noqa: E402


def _write(root: Path, relative: str, source: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


# --------------------------------------------------------------------------- #
# 仓库现状
# --------------------------------------------------------------------------- #
def test_repository_has_no_finding() -> None:
    """当前仓库静态派生检查必须 0 发现。"""
    assert mr.check_static() == []


def test_main_exit_code_zero_on_clean_repo() -> None:
    # 直接调 run_all（不解析 argv），避免 pytest 的 --full 等参数串进来
    assert mr.run_all() == []


def test_coverage_source_is_single_source() -> None:
    """覆盖面清单必须从 architecture_check 导入，不许本脚本另存一份。"""
    assert mr.LAYERS
    assert mr.NON_LAYER_DIRS
    # 关键点：merge_readiness 没有自己的 CODE_DIRS 常量（那会重蹈清单腐烂）
    assert not hasattr(mr, "CODE_DIRS")


# --------------------------------------------------------------------------- #
# D1 字典重复键（_RESOLVE_SQL 事故的直接回归）
# --------------------------------------------------------------------------- #
def test_detects_duplicate_dict_key(tmp_path: Path) -> None:
    """字典字面量重复键必须被点名（含行号与键名）。"""
    _write(
        tmp_path,
        "packages/identity/ownership.py",
        'M = {\n    "Shot": "a",\n    "Scene": "b",\n    "Shot": "c",\n}\n',
    )
    findings = mr.check_static(tmp_path)
    d1 = [f for f in findings if f.rule == "D1 字典重复键"]
    assert len(d1) == 1
    assert "'Shot'" in d1[0].message
    assert d1[0].location.endswith(":4")  # 重复键在第 4 行（被覆盖的那个）


def test_dict_with_distinct_keys_is_clean(tmp_path: Path) -> None:
    """负向的负向：键不重复时不得误报（避免闸门变成天天红的噪音）。"""
    _write(tmp_path, "packages/x.py", 'M = {"a": 1, "b": 2}\n')
    assert mr.check_static(tmp_path) == []


def test_expression_keys_do_not_false_positive(tmp_path: Path) -> None:
    """变量/表达式键无法静态判等，必须跳过而不是制造假红。"""
    _write(tmp_path, "packages/x.py", 'k = "a"\nM = {k: 1, "b": 2}\n')
    assert mr.check_static(tmp_path) == []


# --------------------------------------------------------------------------- #
# D2 同名顶层定义
# --------------------------------------------------------------------------- #
def test_detects_duplicate_toplevel_definition(tmp_path: Path) -> None:
    """同模块两个同名顶层函数必须被点名。"""
    _write(
        tmp_path,
        "services/x.py",
        "def build():\n    return 1\n\ndef build():\n    return 2\n",
    )
    findings = mr.check_static(tmp_path)
    d2 = [f for f in findings if f.rule == "D2 同名顶层定义"]
    assert len(d2) == 1
    assert "build" in d2[0].message


def test_nested_same_name_is_not_flagged(tmp_path: Path) -> None:
    """不同作用域的同名函数不算覆盖，不得误报。"""
    _write(
        tmp_path,
        "services/x.py",
        "def outer():\n    def build(): return 1\n    return build\n"
        "def build(): return 2\n",
    )
    assert [f for f in mr.check_static(tmp_path) if f.rule == "D2 同名顶层定义"] == []


# --------------------------------------------------------------------------- #
# D0 语法解析 fail-closed
# --------------------------------------------------------------------------- #
def test_syntax_error_is_not_silently_skipped(tmp_path: Path) -> None:
    """解析失败必须报红，不能静默跳过（否则坏文件天然免疫检查）。"""
    _write(tmp_path, "packages/broken.py", "def broken(:\n")
    findings = mr.check_static(tmp_path)
    assert any(f.rule == "D0 语法解析" for f in findings)


# --------------------------------------------------------------------------- #
# R0 覆盖面防呆（多分支引入新目录只存在于一条线）
# --------------------------------------------------------------------------- #
def test_detects_unregistered_code_directory(tmp_path: Path) -> None:
    """含 .py 但未在 architecture_check 登记的顶层目录必须报红。"""
    _write(tmp_path, "brand_new_layer/impl.py", "X = 1\n")
    findings = mr.check_static(tmp_path)
    r0 = [f for f in findings if f.rule == "R0 覆盖面"]
    assert len(r0) == 1
    assert "brand_new_layer" in r0[0].message


def test_registered_directories_are_not_flagged(tmp_path: Path) -> None:
    """已登记目录（LAYERS ∪ NON_LAYER_DIRS）里的 .py 不得被 R0 误报。

    R0 只针对「未登记的新目录」——对齐 architecture_check.check_layer_coverage
    的判据。已登记目录的 .py 由 AST 检查（D1/D2/D0）负责，不是覆盖面问题。
    """
    _write(tmp_path, "tests/helper.py", "X = 1\n")
    _write(tmp_path, "infra/stray.py", "X = 1\n")
    assert [f for f in mr.check_static(tmp_path) if f.rule == "R0 覆盖面"] == []


# --------------------------------------------------------------------------- #
# 编排器 fail-closed
# --------------------------------------------------------------------------- #
def test_external_check_records_nonzero_exit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """被编排的检查器返回非零时，必须记为发现（fail-closed，不吞错）。"""

    class FakeCompleted:
        returncode = 1
        stdout = "SIMULATED FAILURE line 1\nSIMULATED FAILURE line 2"
        stderr = ""

    def fake_run(*args: object, **kwargs: object) -> FakeCompleted:
        return FakeCompleted()

    monkeypatch.setattr(mr.subprocess, "run", fake_run)
    findings = mr.run_external_checks(tmp_path)
    assert findings
    assert all(f.rule == "C CI 口径" for f in findings)
    assert len(findings) == len(mr.CHECK_COMMANDS)  # 每项都失败，一项一条


# --------------------------------------------------------------------------- #
# P2-01：git 分支新鲜度（第二轮审查发现的 revision range 写反 + fail-open）
# --------------------------------------------------------------------------- #
def _git(root: Path, *args: str) -> None:
    import subprocess

    subprocess.run(("git", *args), cwd=root, check=True, capture_output=True)


def _make_git_repo(root: Path) -> None:
    """造一个含 origin/main 引用与一个 feature 分支的最小 git 仓库。"""
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "test")
    (root / "seed.txt").write_text("init\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "init")
    _git(root, "branch", "-M", "main")
    # origin/main 就指向当前 main
    _git(root, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git(root, "checkout", "-qb", "feature")


def _g2(findings: list[object]) -> list[object]:
    return [f for f in findings if getattr(f, "rule", "") == "G2 分支新鲜度"]


def test_git_ahead_branch_is_not_falsely_reported_as_behind(tmp_path: Path) -> None:
    """feature 领先 origin/main（正常开发提交）不得被误报为落后——写反方向的回归。"""
    _make_git_repo(tmp_path)
    (tmp_path / "work.txt").write_text("feature work\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "feature work")
    assert _g2(mr.check_git(tmp_path)) == [], "领先提交不该被当成落后"


def test_git_behind_branch_is_flagged(tmp_path: Path) -> None:
    """分支真正落后 origin/main 时必须报红（写反方向时这条会漏）。"""
    _make_git_repo(tmp_path)
    # 在 main 上多提交，然后把 origin/main 指过去；feature 停在原地
    _git(tmp_path, "checkout", "-q", "main")
    (tmp_path / "main.txt").write_text("upstream work\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "upstream work")
    _git(tmp_path, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git(tmp_path, "checkout", "-q", "feature")
    findings = _g2(mr.check_git(tmp_path))
    assert findings, "落后 1 个提交必须报红"
    assert "落后" in findings[0].message and "1" in findings[0].message


def test_git_dirty_worktree_is_flagged(tmp_path: Path) -> None:
    """脏工作树必须报红（G1，独立判据）。"""
    _make_git_repo(tmp_path)
    (tmp_path / "untracked.txt").write_text("dirty\n", encoding="utf-8")
    findings = [f for f in mr.check_git(tmp_path) if getattr(f, "rule", "").startswith("G1")]
    assert findings, "未跟踪文件应让工作树判红"


def test_git_missing_remote_ref_fails_closed(tmp_path: Path) -> None:
    """origin/main 引用缺失必须 fail-closed，不能静默当作「不落后」。"""
    _make_git_repo(tmp_path)
    _git(tmp_path, "update-ref", "-d", "refs/remotes/origin/main")
    findings = _g2(mr.check_git(tmp_path))
    assert findings, "远端引用缺失时静默通过等于 fail-open"
    assert "不存在" in findings[0].message or "fetch" in findings[0].message
