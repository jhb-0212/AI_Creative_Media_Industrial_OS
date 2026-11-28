"""WP-01 / WP-02 的声明与迁移回归：生命周期元数据必须自洽，三处迁移不得再删角色。"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from packages.shared.role_specs import (
    RoleSpec,
    registry_payload,
    role_specs,
    spec_problems,
)

_REPO = Path(__file__).resolve().parents[2]
_MIGRATIONS = _REPO / "data" / "migrations" / "versions"
_MIGRATED_ROLE_FILES = (
    "9e3b4c5d6e7f_create_app_role_and_grants.py",
    "7a8b9c0d1e2f_create_outbox_relay_role.py",
    "c5d6e7f8a9b0_dead_letter_replays.py",
)


def test_declared_specs_are_self_consistent() -> None:
    """声明自身不能有提权属性 / NOLOGIN 配口令 / 未知生命周期取值。"""
    specs = role_specs()
    assert len(specs) == 20
    assert spec_problems(specs) == []


def test_lifecycle_metadata_is_mandatory_per_role() -> None:
    specs = role_specs()
    for spec in specs:
        assert spec.lifecycle_owner == "bootstrap"
        assert spec.lifecycle_state == "active"
        assert spec.deletion_policy == "bootstrap_retire_only"
        assert spec.role_type in {"LOGIN", "NOLOGIN"}
        assert spec.can_be_dropped_by_migration is False


def test_nologin_placeholders_have_no_password_and_no_domain_grants() -> None:
    """``svc_*`` 是 R16 占位：不可登录、无口令来源，避免"有账号没人负责"。"""
    placeholders = [spec for spec in role_specs() if spec.name.startswith("svc_")]
    assert len(placeholders) == 8
    for spec in placeholders:
        assert spec.login is False
        assert spec.password_setting is None
        assert spec.database_domains == ()
        assert spec.migration_refs == ()


def test_domain_roles_carry_domain_and_migration_history() -> None:
    by_name = {spec.name: spec for spec in role_specs()}
    assert by_name["project_planning_role"].database_domains == ("project_planning",)
    assert by_name["asset_service_role"].database_domains == ("asset_service",)
    assert by_name["outbox_ops_role"].migration_refs == ("c5d6e7f8a9b0",)
    assert by_name["outbox_relay_role"].migration_refs == ("7a8b9c0d1e2f",)
    assert by_name["app_role"].migration_refs == ("9e3b4c5d6e7f",)


def test_maintenance_login_is_member_of_operator_group() -> None:
    """方向是 ``GRANT operator TO login``：login 是 operator 组成员。"""
    by_name = {spec.name: spec for spec in role_specs()}
    assert by_name["qc_maintenance_login"].member_of == ("qc_maintenance_operator",)
    assert by_name["qc_maintenance_operator"].member_of == ()


def test_retired_role_must_not_be_declared() -> None:
    assert "qc_worker" not in {spec.name for spec in role_specs()}


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("superuser", True),
        ("bypassrls", True),
        ("createdb", True),
        ("createrole", True),
        ("inherit", False),
        ("lifecycle_owner", "unknown"),
        ("lifecycle_state", "retired"),
        ("deletion_policy", "migration_owned"),
    ],
)
def test_spec_problems_catch_each_violation(field: str, value: object) -> None:
    broken: list[RoleSpec] = [replace(role_specs()[0], **{field: value})]
    problems = spec_problems(tuple(broken))
    assert problems, f"{field}={value} 应被判为声明缺陷"


def test_nologin_role_with_password_is_reported() -> None:
    spec = role_specs()[5]
    broken = replace(spec, login=False, password_setting="postgres_app_password")
    assert spec_problems((broken,))


def test_registry_payload_is_projection_not_source() -> None:
    payload = registry_payload()
    assert len(payload) == len(role_specs())
    assert {row["name"] for row in payload} == {spec.name for spec in role_specs()}
    required = {
        "name",
        "role_type",
        "lifecycle_owner",
        "lifecycle_state",
        "login",
        "superuser",
        "createdb",
        "createrole",
        "bypassrls",
        "inherit",
        "member_of",
        "database_domains",
        "deletion_policy",
        "adr_refs",
        "migration_refs",
    }
    assert required <= set(payload[0])


@pytest.mark.parametrize("filename", _MIGRATED_ROLE_FILES)
def test_migrations_no_longer_drop_or_create_roles(filename: str) -> None:
    """WP-02：三个迁移只断言角色存在，不得再 CREATE / ALTER / DROP 角色。

    只看**执行用的字符串常量**（排除 docstring）：注释里解释"不再 CREATE ROLE"
    属于说明，不是动作。用 AST 而不是行扫描，避免把注释/文档字符串判成 DDL。
    """
    import ast

    path = _MIGRATIONS / filename
    source = path.read_text(encoding="utf-8")
    assert "assert_role_present" in source
    tree = ast.parse(source)
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    executed = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]
    for sql in executed:
        for keyword in ("CREATE ROLE", "ALTER ROLE", "DROP ROLE"):
            assert keyword not in sql, f"{filename} 仍在执行 {keyword}：{sql[:80]}"
