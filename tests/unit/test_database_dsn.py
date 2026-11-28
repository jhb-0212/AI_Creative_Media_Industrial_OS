"""DSN 构造的编码语义测试。

regression：userinfo 曾用 ``quote_plus``，空格被编码成 ``+``；而 URL 的
userinfo 中 ``+`` 是字面字符（不像 query 那样代表空格），密码含空格时
凭据会被解错，导致连接失败且难以定位。
"""

from __future__ import annotations

from types import SimpleNamespace

from pydantic import SecretStr

import packages.shared.database as database


def _settings(password: str, *, master: bool = False) -> SimpleNamespace:
    """构造最小 settings 替身（真实 Settings 是 frozen，不能直接改字段）。

    password 参数在非 master 时同时作为各域角色的密码（都走该值），
    master=True 时则各域角色用固定占位、postgres 用 password。
    """
    return SimpleNamespace(
        postgres_host="db",
        postgres_port=15432,
        postgres_db="ai_film_industrial",
        postgres_user="postgres",
        postgres_password=SecretStr(password if master else "master"),
        postgres_app_user="app_role",
        postgres_app_password=SecretStr(password if not master else "app"),
        postgres_relay_user="outbox_relay_role",
        postgres_relay_password=SecretStr(password if not master else "relay"),
        postgres_project_planning_user="project_planning_role",
        postgres_project_planning_password=SecretStr(
            password if not master else "project_planning"
        ),
        postgres_asset_service_user="asset_service_role",
        postgres_asset_service_password=SecretStr(password if not master else "asset_service"),
    )


def test_app_url_encodes_space_as_percent20(monkeypatch) -> None:
    monkeypatch.setattr(database, "get_settings", lambda: _settings("p a s s"))

    url = database.build_database_url()

    assert "p%20a%20s%20s" in url
    assert "p+a+s+s" not in url


def test_migration_url_encodes_space_as_percent20(monkeypatch) -> None:
    monkeypatch.setattr(database, "get_settings", lambda: _settings("m a s t e r", master=True))

    url = database.build_migration_url()

    assert "m%20a%20s%20t%20e%20r" in url
    assert "m+a+s+t+e+r" not in url


def test_relay_url_uses_dedicated_role(monkeypatch) -> None:
    """relay 使用专用角色 outbox_relay_role（进程边界隔离，非 app_role）。"""
    monkeypatch.setattr(database, "get_settings", lambda: _settings("relay-pass"))

    url = database.build_relay_database_url()

    assert "outbox_relay_role:relay-pass" in url
    assert "app_role" not in url


def test_relay_url_encodes_space_as_percent20(monkeypatch) -> None:
    """relay 密码含空格时按 userinfo 语义编码为 %20（与 app/migration 一致）。"""
    monkeypatch.setattr(database, "get_settings", lambda: _settings("r e l a y"))

    url = database.build_relay_database_url()

    assert "r%20e%20l%20a%20y" in url
    assert "r+e+l+a+y" not in url


def test_project_planning_url_uses_dedicated_role(monkeypatch) -> None:
    """project_planning 域使用专用角色（DATA-1 模块级角色隔离）。"""
    monkeypatch.setattr(database, "get_settings", lambda: _settings("pp-pass"))

    url = database.build_project_planning_database_url()

    assert "project_planning_role:pp-pass" in url
    assert "asset_service_role" not in url


def test_asset_service_url_uses_dedicated_role(monkeypatch) -> None:
    """asset_service 域使用专用角色（DATA-1 模块级角色隔离）。"""
    monkeypatch.setattr(database, "get_settings", lambda: _settings("as-pass"))

    url = database.build_asset_service_database_url()

    assert "asset_service_role:as-pass" in url
    assert "project_planning_role" not in url


def test_domain_urls_encodes_space_as_percent20(monkeypatch) -> None:
    """业务域角色密码含空格时按 userinfo 语义编码为 %20。"""
    monkeypatch.setattr(database, "get_settings", lambda: _settings("d o m a i n"))

    pp_url = database.build_project_planning_database_url()
    as_url = database.build_asset_service_database_url()

    assert "d%20o%20m%20a%20i%20n" in pp_url
    assert "d+o+m+a+i+n" not in pp_url
    assert "d%20o%20m%20a%20i%20n" in as_url
    assert "d+o+m+a+i+n" not in as_url
