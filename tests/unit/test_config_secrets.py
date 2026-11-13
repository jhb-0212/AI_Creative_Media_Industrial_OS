"""packages/shared/config 密钥运行时校验的单元测试（红线 6：密钥不落日志）。

覆盖：
- 生产环境（APP_ENV=production）缺关键密钥 → 拒绝启动（抛 ValidationError）。
- 非生产环境（development）允许空密钥，便于本地联调。
- 密钥字段为 SecretStr，repr/str 不含明文。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from packages.shared.config import Settings

# 生产环境校验所覆盖的「必需密钥」清单（与 Settings._PRODUCTION_REQUIRED_SECRETS 对齐）。
REQUIRED_ENV_VARS = [
    "POSTGRES_PASSWORD",
    "S3_ACCESS_KEY_ID",
    "S3_SECRET_ACCESS_KEY",
    "MQ_URL",
    "MODEL_GATEWAY_API_KEY",
]


def _prod_env() -> dict[str, str]:
    """构造一份生产环境所需的完整环境变量（密钥均为非空）。"""
    return {
        "APP_ENV": "production",
        "POSTGRES_PASSWORD": "s3cret-db",
        "S3_ACCESS_KEY_ID": "AKIAEXAMPLE",
        "S3_SECRET_ACCESS_KEY": "s3cret-s3",
        "MQ_URL": "amqp://user:pass@mq:5672/",
        "MODEL_GATEWAY_API_KEY": "sk-model",
    }


def _make_settings(**overrides: str) -> Settings:
    """构造 Settings 实例，显式禁用 .env 文件加载。

    测试环境可能残留本地 .env（含占位值），若不屏蔽会污染用例：
    monkeypatch.delenv 之后 .env 仍会补回 postgres 等默认值，
    导致「缺失密钥应拒绝」的断言失效。这里强制 _env_file=None，
    使配置只来源于进程环境变量，保证用例确定性。
    """
    return Settings(_env_file=None, **overrides)


def test_production_with_all_secrets_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    """生产环境密钥齐全 → 正常实例化。"""
    for key, value in _prod_env().items():
        monkeypatch.setenv(key, value)
    settings = _make_settings()
    assert settings.app_env == "production"
    assert settings.postgres_password.get_secret_value() == "s3cret-db"


@pytest.mark.parametrize("missing_var", REQUIRED_ENV_VARS)
def test_production_missing_secret_rejected(
    monkeypatch: pytest.MonkeyPatch, missing_var: str
) -> None:
    """生产环境缺少任一必需密钥 → 拒绝启动，并指明缺失的环境变量名。"""
    env = _prod_env()
    env.pop(missing_var)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    # 确保被删的变量在环境中不存在（避免残留真实值）。
    monkeypatch.delenv(missing_var, raising=False)

    with pytest.raises(ValidationError) as exc_info:
        _make_settings()
    # 错误信息中应包含缺失变量名，便于运维定位。
    assert missing_var in str(exc_info.value)


def test_development_allows_empty_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    """非生产环境允许空密钥，不阻断本地联调。"""
    monkeypatch.setenv("APP_ENV", "development")
    for var in REQUIRED_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    settings = _make_settings()
    assert settings.app_env == "development"
    assert settings.postgres_password.get_secret_value() == ""


def test_secret_fields_do_not_leak_in_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    """SecretStr 字段在 repr/str 中不泄露明文（红线 6）。"""
    for key, value in _prod_env().items():
        monkeypatch.setenv(key, value)
    settings = _make_settings()
    rendered = repr(settings)
    assert "s3cret-db" not in rendered
    assert "s3cret-s3" not in rendered
