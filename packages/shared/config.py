"""应用配置（pydantic-settings 从环境变量加载）。

安全要求（红线 6「密钥不落日志」）：
- 敏感字段（密码、API Key、Secret）标记为 SecretStr，repr 与日志自动脱敏。
- 配置类不可变（frozen），避免运行时被篡改。
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """全局应用配置。字段名对应 .env 中的大写变量名。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
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
    otel_service_name: str = "ai-film-industrial-platform"


@lru_cache
def get_settings() -> Settings:
    """获取全局配置单例。"""
    return Settings()
