"""ADR-R46 §8 裁决 3：``e1f2a3b4c5d6`` 降级必须有显式前置守卫。

口径（裁决原文见 ADR-R46 §8 / 附录 C）：
1. 守卫必须在**任何**破坏性 DDL 之前执行（隐式 ``VALIDATE`` 失败只作第二道防线）；
2. 错误文案必须可检索（``[DOWNGRADE_BLOCKED:e1f2a3b4c5d6]``）且给出处置指引，
   不能只留一句"失败"；
3. 守卫只读，不改数据、不改 status——不得用"顺手修数据"换降级成功。
"""

from __future__ import annotations

import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_MIGRATION = (
    _ROOT
    / "data"
    / "migrations"
    / "versions"
    / "e1f2a3b4c5d6_asset_registration_saga_schema.py"
)
_GUARD = "_assert_no_inflight_tasks"
_TAG = "[DOWNGRADE_BLOCKED:e1f2a3b4c5d6]"


def _source() -> str:
    return _MIGRATION.read_text(encoding="utf-8")


def _module() -> ast.Module:
    return ast.parse(_source())


def _func(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} 不存在于 {_MIGRATION.name}")


def test_downgrade_calls_guard_before_any_ddl() -> None:
    downgrade = _func(_module(), "downgrade")
    first = downgrade.body[0]
    assert isinstance(first, ast.Expr), "downgrade 首条语句必须是守卫调用"
    call = first.value
    assert isinstance(call, ast.Call)
    assert isinstance(call.func, ast.Name) and call.func.id == _GUARD


def test_guard_is_read_only_and_targets_inflight_status() -> None:
    guard = _func(_module(), _GUARD)
    text = ast.unparse(guard)
    assert "count(*)" in text
    assert "WHERE status = :status" in text
    assert "_INFLIGHT_TASK_STATUS" in text
    assert 'AWAITING_ASSET_REGISTRATION' in _source()
    # 只读：守卫内不得出现任何 DML/DDL 关键字
    for keyword in ("UPDATE ", "DELETE ", "INSERT ", "ALTER ", "DROP ", "CREATE "):
        assert keyword not in text.upper(), f"守卫必须是只读的，出现了 {keyword!r}"


def test_guard_message_is_tagged_and_actionable() -> None:
    guard = _func(_module(), _GUARD)
    raises = [
        node
        for node in ast.walk(guard)
        if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call)
    ]
    assert raises, "守卫必须显式 raise，不能只靠数据库 VALIDATE 失败"
    rendered = ast.unparse(raises[0])
    assert _TAG in rendered, f"错误文案必须含检索前缀 {_TAG}"
    assert "处置" in rendered, "错误文案必须给出处置指引"


def test_guard_reports_the_blocking_count() -> None:
    """报错要能定位规模，便于判断是"个别遗留"还是"系统性未收敛"。"""
    guard = _func(_module(), _GUARD)
    rendered = ast.unparse(guard)
    assert "条 generation_tasks" in rendered


def _load_migration():
    import importlib.util

    spec = importlib.util.spec_from_file_location("e1f2_guard_probe", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Result:
    def __init__(self, value: int) -> None:
        self._value = value

    def scalar_one(self) -> int:
        return self._value


class _Bind:
    def __init__(self, count: int) -> None:
        self.count = count
        self.calls: list[tuple[str, object]] = []

    def execute(self, statement: object, params: object = None) -> _Result:
        self.calls.append((str(statement), params))
        return _Result(self.count)


class _Op:
    def __init__(self, bind: _Bind) -> None:
        self._bind = bind

    def get_bind(self) -> _Bind:
        return self._bind


def test_guard_raises_when_inflight_rows_exist(monkeypatch) -> None:
    module = _load_migration()
    bind = _Bind(2)
    monkeypatch.setattr(module, "op", _Op(bind))

    import pytest

    with pytest.raises(RuntimeError) as excinfo:
        module._assert_no_inflight_tasks()

    message = str(excinfo.value)
    assert _TAG in message
    assert "2 条" in message
    assert "处置" in message
    # 只查询、不带 DML；参数固定为在途状态
    assert bind.calls and bind.calls[0][1] == {"status": "AWAITING_ASSET_REGISTRATION"}


def test_guard_is_noop_when_no_inflight_rows(monkeypatch) -> None:
    module = _load_migration()
    bind = _Bind(0)
    monkeypatch.setattr(module, "op", _Op(bind))

    module._assert_no_inflight_tasks()
    assert bind.calls and bind.calls[0][1] == {"status": "AWAITING_ASSET_REGISTRATION"}
