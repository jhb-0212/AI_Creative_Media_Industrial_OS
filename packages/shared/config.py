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
    # 应用角色（RLS 生效前提：非超级用户、NOBYPASSRLS）。应用与测试以该角色
    # 连接数据库，使 PostgreSQL RLS 行级安全真正执行（postgres 超级用户会
    # BYPASSRLS 导致 RLS 失效）。迁移 9e3b4c5d6e7f 创建该角色。
    postgres_app_user: str = "app_role"
    postgres_app_password: SecretStr = SecretStr("app_dev_password")

    # Redis（可能内嵌口令，按密钥处理，repr 不落明文）
    redis_url: SecretStr = SecretStr("redis://localhost:16379/0")

    # 限流（Redis 固定窗口）。默认开启：限流是保护性设施，宁可本地多一次
    # 连接尝试，也不要在生产漏配时静默无限流。后端不可用时按 fail-open 放行，
    # 并计入 rate_limit_backend_errors_total。
    rate_limit_enabled: bool = True
    rate_limit_per_minute: int = 120
    rate_limit_window_seconds: int = 60

    # Outbox relay（R06 消费端，workers/relay）。批量与节奏按积压调，不做自适应：
    # 自适应调节会把「行为随负载变化」引入排障面，当前规模用固定参数更可控。
    outbox_relay_batch_size: int = 100
    outbox_relay_interval_seconds: float = 5.0

    # 外发 webhook（relay 的下游）。URL 为空 = 不注册处理器，事件保持未投递：
    # 「没有下游」必须表现为积压，而不是把事件标成已投递。
    # 配了 URL 就必须配密钥：无签名的外发端点等于任何人都能伪造事件。
    outbox_webhook_url: str = ""
    outbox_webhook_secret: SecretStr = SecretStr("")
    outbox_webhook_timeout_seconds: float = 5.0

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

    # 身份治理（决策 4：RS256 JWT）
    # issuer / audience 严格校验，拒绝跨服务令牌；密钥来源默认从环境变量
    # 加载 PEM（生产应从密钥管理服务注入，见 services/identity_access/keys.py）。
    identity_issuer: str = "ai-film-identity"
    identity_audience: str = "ai-film-api"
    identity_access_token_ttl_seconds: int = 900
    # 私钥 PEM（签发用，仅身份服务持有；API 层不接触）。生产环境必填。
    identity_private_key_pem: SecretStr = SecretStr("")
    # 公钥 PEM（验签用，可安全分发）。生产环境必填。
    identity_public_key_pem: SecretStr = SecretStr("")

    # 可观测
    otel_exporter_otlp_endpoint: str = ""
    otel_exporter_otlp_insecure: bool = False
    otel_service_name: str = "ai-film-industrial-platform"

    # 生产环境强制非空的敏感字段（密钥不落日志，且严禁空密钥误上线）。
    # 约定：这些字段在 production 下必须显式配置真实值，否则拒绝启动。
    _PRODUCTION_REQUIRED_SECRETS: tuple[tuple[str, str], ...] = (
        ("postgres_password", "POSTGRES_PASSWORD"),
        ("postgres_app_password", "POSTGRES_APP_PASSWORD"),
        ("identity_private_key_pem", "IDENTITY_PRIVATE_KEY_PEM"),
        ("identity_public_key_pem", "IDENTITY_PUBLIC_KEY_PEM"),
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
        # 显式拒绝开发默认值：迁移 9e3b4c5d6e7f 创建的 app_dev_password
        # 只允许本地/测试使用，不得带入生产（否则 RLS 应用角色的口令可预测）。
        if self.postgres_app_password.get_secret_value() == "app_dev_password":
            raise ValueError(
                "生产环境（APP_ENV=production）不得使用开发默认应用数据库密码"
                " app_dev_password（POSTGRES_APP_PASSWORD），请由部署系统注入独立密码。"
            )
        return self


    @model_validator(mode="after")
    def _require_webhook_secret_with_url(self) -> Settings:
        """配了 webhook URL 却不配密钥时拒绝启动（所有环境，不只生产）。

        理由：无签名的外发端点无法被消费方区分「平台发的」与「别人伪造的」，
        这类配置错误一旦上线就是安全隐患，因此在启动期直接失败，而不是留在
        运行期靠人发现。
        """
        if self.outbox_webhook_url and not self.outbox_webhook_secret.get_secret_value():
            raise ValueError(
                "配置了 OUTBOX_WEBHOOK_URL 但缺少 OUTBOX_WEBHOOK_SECRET："
                "拒绝启动未经签名的事件外发。"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    """获取全局配置单例。"""
    return Settings()
