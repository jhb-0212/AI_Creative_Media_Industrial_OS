"""F3 裁决后的 #24 身份生命周期边界测试（WP-04）。

裁决依据：docs/audits/2026-11-27-wp04-d08-f1-f3-options.md（F3）+ WP-02 边界：
Bootstrap 拥有角色创建/生命周期，Migration 不拥有基础角色删除权。
"""

from __future__ import annotations

import ast
from pathlib import Path

from packages.shared import role_specs

REPO_ROOT = Path(__file__).resolve().parents[2]
PILOT = REPO_ROOT / "data" / "migrations" / "versions" / "8f3b2c1d4e5a_split_domain_roles.py"
BOOTSTRAP = REPO_ROOT / "scripts" / "bootstrap_identity.py"
D08 = REPO_ROOT / "docs" / "audits" / "2026-11-27-wp04-d08-f1-f3-options.md"


def _source() -> str:
    return PILOT.read_text(encoding="utf-8")


def _sql_strings() -> list[str]:
    tree = ast.parse(_source())
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.append(node.value)
    return out


def test_pilot_has_no_drop_role_statement() -> None:
    statements = [s for s in _sql_strings() if s.strip().upper().startswith("DROP ROLE")]
    assert statements == []
    # 注释里也不能残留可执行的 DROP ROLE 模板
    assert "DROP ROLE IF EXISTS" not in _source()


def test_pilot_revokes_cross_schema_default_privileges() -> None:
    text = _source().upper()
    assert "ALTER DEFAULT PRIVILEGES" in text
    # 交叉默认权限：授给对端域角色，必须在对端 schema 撤销
    assert "REVOKE" in text
    assert "ON TABLES FROM" in text
    assert "ON SEQUENCES FROM" in text


def test_pilot_revokes_public_schema_usage() -> None:
    text = _source().upper()
    assert "REVOKE USAGE ON SCHEMA PUBLIC" in text


def test_pilot_created_roles_remain_declared_identity() -> None:
    declared = {spec.name for spec in role_specs.role_specs()}
    retired = role_specs.retired_role_names()
    # #24 建的两个域角色是活角色，不能同时出现在退役名单
    for name in ("project_planning_role", "asset_service_role"):
        assert name in declared
        assert name not in retired


def test_bootstrap_does_not_resurrect_retired_roles() -> None:
    text = BOOTSTRAP.read_text(encoding="utf-8")
    assert "RETIRED_ROLES" in text
    # 退役角色必须被过滤，而不是无条件 CREATE ROLE
    assert "retired" in text.lower()


def test_f3_decision_is_recorded_in_d08() -> None:
    text = D08.read_text(encoding="utf-8")
    assert "F3" in text
    assert "RETIRED_ROLES" in text

