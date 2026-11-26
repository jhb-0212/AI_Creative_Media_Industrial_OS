"""``role_specs.diff_role_attributes`` 的判定单测（不连库）。

为什么判定要单独测
------------------
这是 AUTHZ-7 债务期的唯一漂移闸门：它报 PASS 才会让我们安心地把「角色属性
没变」当作既成事实。判太松 → 真的有人把 svc_asset 提成 SUPERUSER，闸门不响；
判太严 → 集群里多一个运维手工建的只读身份就整片红，闸门被忽略。四类失效各
用一条构造用例钉住，最后一条专门证明「额外角色不参与判定」。
"""

from __future__ import annotations

from packages.shared.role_specs import LiveRole, RoleSpec, diff_role_attributes


def _spec(name: str, *, login: bool = True) -> RoleSpec:
    return RoleSpec(
        name=name, login=login, password_setting=None, purpose="测试用身份"
    )


def _live(name: str, *, login: bool = True, **flags: bool) -> LiveRole:
    return LiveRole(
        name=name,
        login=login,
        superuser=flags.get("superuser", False),
        create_db=flags.get("create_db", False),
        create_role=flags.get("create_role", False),
        bypass_rls=flags.get("bypass_rls", False),
        inherit=flags.get("inherit", True),
    )


def test_clean_cluster_has_no_problems() -> None:
    specs = (_spec("svc_asset"), _spec("qc_maintenance_owner", login=False))
    live = (_live("svc_asset"), _live("qc_maintenance_owner", login=False))
    assert diff_role_attributes(specs, live) == []


def test_missing_declared_role_is_a_problem() -> None:
    problems = diff_role_attributes((_spec("svc_asset"),), ())
    assert len(problems) == 1
    assert "声明存在但集群里没有" in problems[0]


def test_any_escalation_attribute_is_reported() -> None:
    problems = diff_role_attributes(
        (_spec("svc_asset"),), (_live("svc_asset", bypass_rls=True, superuser=True),)
    )
    # 一条消息里列出全部命中的属性，便于一次修完，而不是修一个再跑一次。
    assert len(problems) == 1
    assert "BYPASSRLS" in problems[0] and "SUPERUSER" in problems[0]


def test_login_mismatch_is_a_problem_both_directions() -> None:
    problems = diff_role_attributes(
        (_spec("qc_maintenance_owner", login=False), _spec("svc_asset")),
        (_live("qc_maintenance_owner", login=True), _live("svc_asset", login=False)),
    )
    assert len(problems) == 2
    assert "可登录性不符" in problems[0]


def test_inherit_off_is_a_problem() -> None:
    problems = diff_role_attributes(
        (_spec("qc_maintenance_login"),), (_live("qc_maintenance_login", inherit=False),)
    )
    assert len(problems) == 1
    assert "INHERIT" in problems[0]


def test_undeclared_extra_role_is_not_a_problem() -> None:
    """集群里的额外身份属盘点项，不该让闸门变红。"""
    problems = diff_role_attributes(
        (_spec("svc_asset"),), (_live("svc_asset"), _live("ops_readonly_manual"))
    )
    assert problems == []
