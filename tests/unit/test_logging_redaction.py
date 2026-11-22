"""日志脱敏单元测试（红线 6「密钥不落日志」）。

覆盖两条路径：
- 键名命中敏感词 → 整值替换；
- DSN/URL 值内嵌 userinfo → 只替换 userinfo（键名不含敏感词时的兜底）。
"""

from packages.shared.logging import _redact_sensitive


def _run(event: dict[str, object]) -> dict[str, object]:
    return _redact_sensitive(None, "info", dict(event))


def test_redacts_value_embedded_dsn_password() -> None:
    """database_url 键名不含 password，但连接串里有口令，必须被脱敏。"""
    out = _run(
        {"event": "db_connect", "database_url": "postgresql://app:secret@db:5432/x"}
    )
    assert out["database_url"] == "postgresql://***REDACTED***@db:5432/x"


def test_redacts_by_sensitive_key_name() -> None:
    assert _run({"password": "p", "token": "t"}) == {
        "password": "***REDACTED***",
        "token": "***REDACTED***",
    }


def test_keeps_plain_url_without_credentials() -> None:
    out = _run({"url": "https://example.com/a?b=1"})
    assert out["url"] == "https://example.com/a?b=1"


def test_redacts_nested_structures() -> None:
    """嵌套 dict/list 内的敏感键同样脱敏（顶层遍历会漏）。"""
    out = _run(
        {
            "event": "cfg",
            "config": {
                "db": {"password": "p", "dsn": "postgresql://u:p@h/db"},
                "chain": [{"secret": "s"}, {"api_key": "k"}],
            },
        }
    )
    config = out["config"]
    assert isinstance(config, dict)
    assert config["db"] == {
        "password": "***REDACTED***",
        "dsn": "postgresql://***REDACTED***@h/db",
    }
    assert config["chain"] == [
        {"secret": "***REDACTED***"},
        {"api_key": "***REDACTED***"},
    ]
