"""应用配置（pydantic-settings 从环境变量加载）。

安全要求（红线 6「密钥不落日志」）：
- 敏感字段（密码、API Key、Secret）标记为 SecretStr，repr 与日志自动脱敏。
- 配置类不可变（frozen），避免运行时被篡改。
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """全局应用配置。字段名对应 .env 中的大写变量名。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        frozen=True,
    )

    # 基础
    app_env: str = "development"
    app_version: str = "2.0.5"
    log_level: str = "INFO"

    # 数据库（密钥脱敏）
    postgres_host: str = "localhost"
    postgres_port: int = 15432
    postgres_user: str = "postgres"
    postgres_password: SecretStr = SecretStr("")
    postgres_db: str = "ai_film_industrial"

    # Redis
    redis_url: str = "redis://localhost:16379/0"

    # 对象存储
    s3_endpoint_url: str = ""
    s3_access_key_id: SecretStr = SecretStr("")
    s3_secret_access_key: SecretStr = SecretStr("")
    s3_bucket: str = "ai-film-assets"

    # Temporal
    temporal_host: str = "localhost"
    temporal_port: int = 7233

    # 消息队列
    mq_url: SecretStr = SecretStr("")

    # 模型网关（双模式计费）
    model_gateway_billing_mode: str = "USER_OWN_KEY"
    model_gateway_api_key: SecretStr = SecretStr("")

    # 可观测
    otel_exporter_otlp_endpoint: str = ""
    otel_exporter_otlp_insecure: bool = False
    otel_service_name: str = "ai-film-industrial-platform"

    # 生产环境强制非空的敏感字段（密钥不落日志，且严禁空密钥误上线）。
    # 约定：这些字段在 production 下必须显式配置真实值，否则拒绝启动。
    _PRODUCTION_REQUIRED_SECRETS: tuple[tuple[str, str], ...] = (
        ("postgres_password", "POSTGRES_PASSWORD"),
        ("s3_access_key_id", "S3_ACCESS_KEY_ID"),
        ("s3_secret_access_key", "S3_SECRET_ACCESS_KEY"),
        ("mq_url", "MQ_URL"),
        ("model_gateway_api_key", "MODEL_GATEWAY_API_KEY"),
    )

    @model_validator(mode="after")
    def _reject_empty_secrets_in_production(self) -> Settings:
        """生产环境（app_env=production）下，关键密钥为空时拒绝启动。

        仅拦截「完全未配置」的空密钥（SecretStr 值为空字符串），
        避免把本地开发默认值带入生产造成安全事故。
        """
        if self.app_env != "production":
            return self
        missing: list[str] = []
        for field_name, env_name in self._PRODUCTION_REQUIRED_SECRETS:
            value = getattr(self, field_name)
            secret = value.get_secret_value() if isinstance(value, SecretStr) else value
            if not secret:
                missing.append(env_name)
        if missing:
            raise ValueError(
                "生产环境（APP_ENV=production）缺少必需密钥，拒绝启动："
                + ", ".join(missing)
            )
        return self


@lru_cache
def get_settings() -> Settings:
    """获取全局配置单例。"""
    return Settings()
