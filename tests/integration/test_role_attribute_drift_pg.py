"""真实集群的角色属性漂移验收（声明 vs pg_roles）。

它证明的不是「代码里有一份声明」，而是「这台集群的二十个身份与声明逐项一致」。
AUTHZ-7（迁移执行者仍是超管）未关闭期间，这是唯一在部署后仍能复验身份面的
东西：迁移只负责「建」，本用例负责「建出来的属性没被改坏」。
"""

from __future__ import annotations

import pytest

from packages.shared.role_specs import diff_role_attributes, role_specs
from scripts.verify_role_attributes import _live_roles


async def test_live_cluster_matches_declared_role_attributes() -> None:
    specs = role_specs()
    live = await _live_roles()

    # 声明里不许出现「集群里没有」的角色：那说明声明是先写后建的意向书，而
    # 不是事实源。反之（集群多出未声明角色）属于盘点项，不在这里判红。
    problems = diff_role_attributes(specs, tuple(live))
    assert problems == [], "角色属性漂移：\n" + "\n".join(problems)


@pytest.mark.parametrize("role_key", ["owner", "operator", "login"])
async def test_maintenance_identities_are_declared(role_key: str) -> None:
    from packages.shared.db_roles import maintenance_role

    names = {spec.name for spec in role_specs()}
    assert maintenance_role(role_key) in names


async def test_retired_worker_is_not_declared() -> None:
    """退役身份不得被重新写进声明（否则闸门会把它当「缺失」永远报红）。"""
    from packages.shared.db_roles import maintenance_role

    names = {spec.name for spec in role_specs()}
    assert maintenance_role("worker") not in names
