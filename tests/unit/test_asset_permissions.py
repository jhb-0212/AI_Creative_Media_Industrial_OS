"""契约主体名可机读校验（ADR-R08 G3 回归锁）。

背景：``allowed_principals`` 是文本字段，解析器按 ``/`` 切分并剥离括号内资质
说明。若把限定语写成 ``导演按职责``（无括号），解析结果会多出一个现实中不存
在的角色名，导致真实「导演」永远无法通过授权——这是沉默的权限死角。
本文件把「资产域 approvals 的主体名必须可机读」固化成回归用例。
"""

from __future__ import annotations

import pytest

from services.identity_access.action_registry import allowed_roles

# 现实身份域里存在的角色名（JWT roles claim 会用到的字面值）。
_KNOWN_ROLES = {
    "Worker",
    "Model Adapter",
    "QC Worker",
    "受信检测服务",
    "授权审核人",
    "QC",
    "导演",
    "制片",
}


@pytest.mark.parametrize(
    "action_key",
    [
        "ASSET.VERSION.CREATE",
        "QC.REPORT.CREATE",
        "ASSET.VERSION.APPROVE",
        "ASSET.VERSION.READ",
    ],
)
def test_asset_action_principals_are_machine_readable(action_key: str) -> None:
    roles = allowed_roles(action_key)
    assert roles, f"{action_key} 没有解析出任何主体"
    unknown = roles - _KNOWN_ROLES
    assert not unknown, (
        f"{action_key} 解析出不可机读的主体名 {sorted(unknown)}；"
        "限定语必须写成括号形式（如「导演（按职责）」）"
    )


def test_approve_allows_director_via_parenthesized_qualifier() -> None:
    """「导演（按职责）」必须解析成「导演」，而不是「导演按职责」。"""
    assert allowed_roles("ASSET.VERSION.APPROVE") == frozenset(
        {"授权审核人", "QC", "导演"}
    )


def test_read_is_low_risk_and_does_not_overlap_write_only_roles() -> None:
    """读主体集合里不应出现只写角色（Worker），避免写角色顺带获得读权限。"""
    assert "Worker" not in allowed_roles("ASSET.VERSION.READ")
