"""维护命令 CLI 的纯单测：参数契约与退出码映射。

真库上的「身份不符即拒绝」由 ``test_qc_identity_startup_pg`` 证明；这里钉的是
脚本可依赖的接口事实：子命令必填、修必修带任务、拒绝与「拒绝但没记账」必须是
两个不同的退出码（定时任务据此分辨「被拒绝」与「账丢了」）。
"""

from __future__ import annotations

import pytest

from workers.qc_maintenance.__main__ import (
    EXIT_DENIAL_NOT_RECORDED,
    EXIT_DENIED,
    EXIT_OK,
    _exit_code,
    _parse_args,
)


def test_reconcile_requires_work_order_only() -> None:
    args = _parse_args(["reconcile", "--work-order", "WO-1"])
    assert args.command == "reconcile"
    assert args.work_order == "WO-1"
    assert args.attempt is None


def test_repair_requires_task_id() -> None:
    with pytest.raises(SystemExit):
        _parse_args(["repair", "--work-order", "WO-1"])


def test_repair_accepts_stable_attempt_id() -> None:
    args = _parse_args(["repair", "--work-order", "WO-1", "--task", "T-1", "--attempt", "a-1"])
    assert args.command == "repair"
    assert (args.work_order, args.task, args.attempt) == ("WO-1", "T-1", "a-1")


def test_detect_takes_no_work_order() -> None:
    args = _parse_args(["detect"])
    assert args.command == "detect"
    assert not hasattr(args, "work_order")


def test_subcommand_is_required() -> None:
    with pytest.raises(SystemExit):
        _parse_args([])


class _Result:
    def __init__(self, denial: str | None, recorded: bool = True) -> None:
        self.denial = denial
        self.denial_recorded = recorded


def test_exit_code_separates_denied_from_unrecorded_denial() -> None:
    assert _exit_code(_Result(None)) == EXIT_OK
    assert _exit_code(_Result("task_not_authorized", recorded=True)) == EXIT_DENIED
    assert _exit_code(_Result("permission_denied", recorded=False)) == EXIT_DENIAL_NOT_RECORDED, (
        "账没记上不能报成普通拒绝：那会把一条丢失的失败事实当成正常结果"
    )
