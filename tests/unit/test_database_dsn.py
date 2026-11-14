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
    """构造最小 settings 替身（真实 Settings 是 frozen，不能直接改字段）。"""
    return SimpleNamespace(
        postgres_host="db",
        postgres_port=15432,
        postgres_db="ai_film_industrial",
        postgres_user="postgres",
        postgres_password=SecretStr(password if master else "master"),
        postgres_app_user="app_role",
        postgres_app_password=SecretStr(password if not master else "app"),
    )


def test_app_url_encodes_space_as_percent20(monkeypatch) -> None:
    monkeypatch.setattr(database, "get_settings", lambda: _settings("p a s s"))

    url = database.build_database_url()

    assert "p%20a%20s%20s" in url
    assert "p+a+s+s" not in url


def test_migration_url_encodes_space_as_percent20(monkeypatch) -> None:
    monkeypatch.setattr(
        database, "get_settings", lambda: _settings("m a s t e r", master=True)
    )

    url = database.build_migration_url()

    assert "m%20a%20s%20t%20e%20r" in url
    assert "m+a+s+t+e+r" not in url
