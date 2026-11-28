"""集成测试目录级基础设施：数据库版本前置条件（C4）。

为什么用 hook 而不是 fixture
----------------------------
要求是「不满足即 FAIL、测试主体不得执行、不得记为 PASS」。若把判定写进
session 级 autouse fixture，失败发生在 setup 阶段，报告是 ERROR 而不是 FAILED；
更糟的是 fixture 的失败容易被读成「环境没起来」，而它实际是「环境起来了、
但版本不对」——这是两种不同的处置动作。

``pytest_pyfunc_call`` 是 firstresult 的调用入口：在这里先判定，不满足就
``pytest.fail``，测试函数体根本不会被执行；满足则返回 ``None`` 交回默认实现。
判定结果按会话缓存，整目录只连一次库。

为什么不给豁免开关
------------------
``ALLOW_STALE_DB`` / ``SKIP_HEAD_CHECK`` 这类开关会让「不满足统一测试前提」的
环境继续产出绿色结果，于是闸门退化成一条注释。本地库落后时的正确流程是
看报错里的两个 revision、执行 ``alembic upgrade head``、再跑测试；需要历史
结构时另立迁移兼容性测试，用独立库和显式目标 revision。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from tests.integration._db_head import (
    DatabaseHeadMismatchError,
    check_database_at_head,
    read_current_revisions,
    repository_heads,
)


@dataclass
class _GateState:
    """会话内的一次性判定结果（先判者写，后来者读）。"""

    checked: bool = False
    failure: str | None = None


_STATE = _GateState()


def _reset_gate() -> None:
    _STATE.checked = False
    _STATE.failure = None


def _evaluate_gate() -> None:
    """连一次库，判定 revision 与仓库 head 是否一致。"""
    if _STATE.checked:
        return
    _STATE.checked = True
    try:
        # 同步 hook 里没有运行中的事件循环（pytest-asyncio 的 loop 尚未驱动），
        # 因此可以在这里独立开一次 loop；读取用的是独立 engine，读完即释放。
        current = asyncio.run(read_current_revisions())
        check_database_at_head(current, repository_heads())
    except DatabaseHeadMismatchError as exc:
        _STATE.failure = str(exc)
    except Exception as exc:  # 环境不可用（连不上、权限不足）必须变成明确红灯
        _STATE.failure = f"无法读取被测库的迁移版本，集成测试拒绝执行：{type(exc).__name__}: {exc}"


def pytest_sessionstart(session: pytest.Session) -> None:
    _reset_gate()


@pytest.hookimpl(tryfirst=True)
def pytest_pyfunc_call(pyfuncitem: pytest.Function) -> object | None:
    """在执行任何集成用例的函数体之前，先确认库在 head。"""
    _evaluate_gate()
    if _STATE.failure:
        pytest.fail(_STATE.failure, pytrace=False)
    return None
