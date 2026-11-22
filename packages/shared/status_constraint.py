"""status 列 CHECK 约束的唯一生成器（契约 → DDL 的单一出口）。

为什么需要这个模块
----------------
契约把 status 的状态集收成枚举之后，数据库必须同样拒绝非法值——否则「契约收紧了、
库里还收任意字符串」，两层的可信度取决于谁先写。手写一条
``CheckConstraint("status IN ('A','B','C')")`` 有个隐蔽的坑：DDL 里的值列表是字符串，
改了契约没人会去改 SQL，而测试用的是 Domain Enum（另一处手写），于是两处一起
偏离契约、一起自洽。

所以 DDL 由本模块从**同一份值集合**生成：

    status_check(ProjectStatus, table="projects")
        → CheckConstraint("status IN ('ACTIVE', 'ARCHIVED', ...)", name="ck_projects_status")

闸门（scripts/contract_check.py::runtime_status_problems）用同一个函数算期望值，
再与模型里真实的约束文本逐字比对。差异只有一个来源：有人改了契约没重算约束。
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import CheckConstraint


def status_check_sql(values: Iterable[str]) -> str:
    """把状态集合渲染成 CHECK 表达式（字母序，便于逐字比对）。"""
    ordered = sorted({str(v) for v in values})
    if not ordered:
        raise ValueError("status 状态集不能为空")
    quoted = ", ".join(f"'{value}'" for value in ordered)
    return f"status IN ({quoted})"


def status_check(values: Iterable[str], *, table: str) -> CheckConstraint:
    """按契约状态集生成 status CHECK 约束（命名固定为 ``ck_<table>_status``）。"""
    return CheckConstraint(status_check_sql(values), name=f"ck_{table}_status")
