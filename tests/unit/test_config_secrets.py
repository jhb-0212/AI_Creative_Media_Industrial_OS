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
#
# B4 修正：``IDENTITY_PRIVATE_KEY_PEM`` 已从清单中移除。它此前被列为生产必填，
# 等于要求每个 API 实例都持有签发私钥——把最危险的部署形态写成了合规要求。
# 现在私钥只允许出现在 ``IDENTITY_PROCESS_ROLE=issuer`` 的进程。
REQUIRED_ENV_VARS = [
    "POSTGRES_PASSWORD",
    "POSTGRES_APP_PASSWORD",
]


def _prod_env() -> dict[str, str]:
    """构造一份生产环境所需的完整环境变量（密钥均为非空）。

    形态是**API 进程**：有验签公钥、没有签发私钥（B4）。
    """
    return {
        "APP_ENV": "production",
        "IDENTITY_PROCESS_ROLE": "api",
        "POSTGRES_PASSWORD": "s3cret-db",
        "POSTGRES_APP_PASSWORD": "prod-app-pass",
        "IDENTITY_PUBLIC_KEY_PEM": "-----BEGIN PUBLIC KEY-----prod-----END PUBLIC KEY-----",
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


def test_production_api_process_rejects_signing_private_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """生产 API 进程配置了签发私钥 → 拒绝启动（B4 的核心要求）。

    这条与"缺密钥拒绝启动"方向相反：多配一把私钥同样是启动失败。
    只做"缺了才拦"的校验，等于允许最高危的配置形态一路绿灯。
    """
    for key, value in _prod_env().items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv(
        "IDENTITY_PRIVATE_KEY_PEM",
        "-----BEGIN PRIVATE KEY-----prod-----END PRIVATE KEY-----",
    )
    with pytest.raises(ValidationError) as exc_info:
        _make_settings()
    assert "不得配置签发私钥" in str(exc_info.value)


def test_issuer_process_requires_private_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """签发进程（role=issuer）没有私钥 → 拒绝启动。"""
    for key, value in _prod_env().items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("IDENTITY_PROCESS_ROLE", "issuer")
    monkeypatch.delenv("IDENTITY_PRIVATE_KEY_PEM", raising=False)
    with pytest.raises(ValidationError) as exc_info:
        _make_settings()
    assert "必须配置" in str(exc_info.value)


def test_secret_fields_do_not_leak_in_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    """SecretStr 字段在 repr/str 中不泄露明文（红线 6）。"""
    for key, value in _prod_env().items():
        monkeypatch.setenv(key, value)
    settings = _make_settings()
    rendered = repr(settings)
    assert "s3cret-db" not in rendered
    assert "s3cret-s3" not in rendered


def test_production_rejects_dev_default_app_password(monkeypatch: pytest.MonkeyPatch) -> None:
    """生产环境显式使用开发默认应用密码 app_dev_password → 拒绝启动。"""
    for key, value in _prod_env().items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("POSTGRES_APP_PASSWORD", "app_dev_password")
    with pytest.raises(ValidationError) as exc_info:
        _make_settings()
    assert "app_dev_password" in str(exc_info.value)


def test_production_does_not_require_unimplemented_adapters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P3-6：S3 / MQ / 模型网关尚无实现，不得列为生产必需。

    强制配置一个没有实现的依赖，只会让运维填假值把启动检查"骗过"；
    真保护来自适配器落地后再登记必需项。
    """
    env = _prod_env()
    for var in (
        "S3_ACCESS_KEY_ID",
        "S3_SECRET_ACCESS_KEY",
        "MQ_URL",
        "MODEL_GATEWAY_API_KEY",
    ):
        env.pop(var, None)
        monkeypatch.delenv(var, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    settings = _make_settings()
    assert settings.app_env == "production"
