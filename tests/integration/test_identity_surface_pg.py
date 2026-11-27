"""身份面基线闸门：活库必须等于 data/migrations/identity_surface_baseline.json。

为什么这条要单独存在
--------------------
``test_role_attribute_drift_pg`` 只盯角色属性；而「重建迁移链」真正容易弄坏的是
授权面：一次 ``REVOKE`` 少写、一个策略的 ``TO`` 角色写错、默认权限没撤——属性
全绿，权限已经悄悄放宽。本用例把九层（角色/成员关系/属主/schema ACL/表 ACL/
列 ACL/默认权限/函数 ACL/策略）一起比，任何一层变了就红。

基线的用法是「只许按迁移语义更新」：重建链完成、确认差异全是预期内的，才重写
基线；平时它是冻结产物。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from scripts.dump_identity_surface import _BASELINE_PATH, _dump_from_settings, diff

pytestmark = pytest.mark.integration


def test_live_surface_matches_baseline() -> None:
    baseline = json.loads(_BASELINE_PATH.read_text(encoding="utf-8"))
    surface = asyncio.run(_dump_from_settings())
    problems = diff(baseline, surface)
    assert problems == [], "身份面与基线不一致（前 10 条）：\n" + "\n".join(problems[:10])
