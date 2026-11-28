"""身份面快照的判据用例。

锁住三件事：
1. ``diff`` 是双向的——只报「左有右无」会漏掉「右多了一层权限」，而那恰好是
   重建迁移链最危险的方向（悄悄放宽）。
2. 基线文件必须与当前集群一致：它是「回放等价」这一结论的存档，不是随手导出。
   基线过期时，后续任何「搬完之后比一比」都会拿一份错账本去比。
3. 各层都不能是空的：某层查询写错（例如字段别名写丢）会退化成空集，从而让
   比对恒等——这比报错更危险。
"""

from __future__ import annotations

import json

from scripts.dump_identity_surface import SECTIONS, diff


def test_diff_reports_both_directions() -> None:
    left = {"roles": [["a", True]], "policies": []}
    right = {"roles": [["b", True]], "policies": []}
    problems = diff(left, right)
    assert any("仅存在于左" in p for p in problems)
    assert any("仅存在于右" in p for p in problems)


def test_diff_detects_extra_privilege_only_on_right() -> None:
    """右侧多一条 GRANT = 放宽了权限；必须报，不能因「左侧没少东西」而放过。"""
    left = {"relation_acl": [["s.t", "SELECT", "app_role", False]]}
    right = {
        "relation_acl": [
            ["s.t", "SELECT", "app_role", False],
            ["s.t", "DELETE", "app_role", False],
        ]
    }
    problems = diff(left, right)
    assert len(problems) == 1
    assert "DELETE" in problems[0]


def test_diff_equal_payloads() -> None:
    payload = {"roles": [["a", True]], "policies": [["x", "Y"]]}
    assert diff(payload, json.loads(json.dumps(payload))) == []


def test_sections_are_the_documented_nine() -> None:
    assert SECTIONS == (
        "roles",
        "memberships",
        "ownership",
        "schema_acl",
        "relation_acl",
        "column_acl",
        "default_acl",
        "function_acl",
        "policies",
    )


def test_baseline_has_no_empty_layer_that_matters() -> None:
    from scripts.dump_identity_surface import _BASELINE_PATH

    baseline = json.loads(_BASELINE_PATH.read_text(encoding="utf-8"))
    for section in SECTIONS:
        assert section in baseline, f"基线缺层：{section}"
        assert baseline[section], f"基线层为空（区分不出「没有」与「查询写错」）：{section}"
