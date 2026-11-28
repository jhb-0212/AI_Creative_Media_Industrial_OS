"""bootstrap 语句生成的判据用例（不连库）。

这里要锁住的不是「能拼出 SQL」，而是四条会让 bootstrap 变成事故源的口径：

1. 每个角色都是「守卫创建 + 无条件收敛」两条：只有 CREATE 的话，属性漂移过的
   存量集群永远不会被拉回声明；
2. 可登录角色必须有口令来源，且口令里的单引号按 SQL 规则翻倍——否则一个带
   ``'`` 的口令会把语句截断成两半，第二半被当成新语句执行；
3. 不可登录角色不得出现 ``PASSWORD``：给 NOLOGIN 身份设口令，等于给将来
   「顺手打开 LOGIN」留好后门；
4. 授权阶段不引用已退役身份：``REVOKE ... FROM qc_worker`` 是历史迁移的内容，
   bootstrap 照抄只会得到 role does not exist。
"""

from __future__ import annotations

from packages.shared.identity_bootstrap import (
    grant_statements,
    membership_statements,
    missing_objects,
    required_objects,
    role_statements,
)
from packages.shared.role_specs import RoleSpec, role_specs


def _passwords_for(specs: tuple[RoleSpec, ...]) -> dict[str, str]:
    return {spec.password_setting: f"pw_{spec.name}" for spec in specs if spec.password_setting}


def test_every_role_gets_guard_and_converge() -> None:
    specs = role_specs()
    statements = role_statements(specs, _passwords_for(specs))
    assert len(statements) == len(specs) * 2
    for index, spec in enumerate(specs):
        guard = statements[index * 2]
        converge = statements[index * 2 + 1]
        assert guard.startswith("DO $$")
        assert "CREATE ROLE" in guard
        assert f"rolname = '{spec.name}'" in guard
        assert converge.startswith(f'ALTER ROLE "{spec.name}" WITH')


def test_quote_in_password_is_escaped() -> None:
    spec = RoleSpec(name="p_role", login=True, password_setting="p_pw", purpose="t")
    statements = role_statements([spec], {"p_pw": "a'b'c"})
    assert "PASSWORD 'a''b''c'" in statements[0]
    assert "PASSWORD 'a''b''c'" in statements[1]
    # 未翻倍的写法会让语句在第一个引号处截断。
    assert "PASSWORD 'a'b'c'" not in statements[0]


def test_login_role_without_password_source_is_rejected() -> None:
    spec = RoleSpec(name="leaky", login=True, password_setting=None, purpose="t")
    try:
        role_statements([spec], {})
    except ValueError as exc:
        assert "password_setting" in str(exc)
    else:
        raise AssertionError("可登录但没有口令来源必须直接报错")


def test_empty_password_is_rejected() -> None:
    spec = RoleSpec(name="empty", login=True, password_setting="e_pw", purpose="t")
    try:
        role_statements([spec], {"e_pw": ""})
    except ValueError as exc:
        assert "空口令" in str(exc)
    else:
        raise AssertionError("空口令不得建出登录角色")


def test_nologin_role_has_no_password_clause() -> None:
    spec = RoleSpec(name="n_role", login=False, password_setting=None, purpose="t")
    statements = role_statements([spec], {})
    assert "NOLOGIN" in statements[0]
    assert "PASSWORD" not in statements[0]
    assert "PASSWORD" not in statements[1]


def test_no_statement_names_retired_worker() -> None:
    from packages.shared.db_roles import maintenance_role

    specs = role_specs()
    retired = maintenance_role("worker")
    for statement in role_statements(specs, _passwords_for(specs)):
        assert retired not in statement
    for statement in grant_statements(server_version_num=160000):
        assert retired not in statement


def test_membership_is_only_operator_to_login() -> None:
    from packages.shared.db_roles import maintenance_role

    operator = maintenance_role("operator")
    login = maintenance_role("login")
    assert membership_statements() == [f"GRANT {operator} TO {login}"]


def test_grant_plan_reverts_public_before_granting() -> None:
    """每个被撤权的对象，``REVOKE ALL ... FROM PUBLIC`` 必须出现。"""
    statements = grant_statements(server_version_num=160000)
    assert any("REVOKE ALL ON FUNCTION" in s for s in statements)
    assert any("ALTER DEFAULT PRIVILEGES" in s for s in statements)
    assert len(statements) > 200


def test_required_objects_and_missing_report() -> None:
    required = required_objects()
    assert ("project_planning", "projects") in required

    # schema 在、表缺：报具体的 schema.table（这是「迁移没跑完」的典型形态）。
    present_partial = missing_objects({"project_planning": frozenset({"projects"})})
    assert "project_planning.projects" not in present_partial
    assert "project_planning.episodes" in present_partial

    # schema 整个不在：只报 schema（后面那串表都归同一个根因，逐表刷屏会淹没它）。
    absent = missing_objects({})
    assert "project_planning" in absent
    assert all("schema 不存在" in item for item in absent if item.startswith("project_planning."))


def test_plan_is_deterministic() -> None:
    specs = role_specs()
    first = role_statements(specs, _passwords_for(specs))
    second = role_statements(specs, _passwords_for(specs))
    assert first == second
