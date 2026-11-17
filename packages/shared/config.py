"""应用配置（pydantic-settings 从环境变量加载）。

安全要求（红线 6「密钥不落日志」）：
- 敏感字段（密码、API Key、Secret）标记为 SecretStr，repr 与日志自动脱敏。
- 配置类不可变（frozen），避免运行时被篡改。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

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
    # 业务域角色口令（统一一份）。
    #
    # 为什么这里**没有** ``postgres_app_user``：连接用户名必须等于业务域角色名
    # （``packages.shared.db_roles`` 的唯一事实源）。一旦用户名可配置，权限
    # 边界就变成了配置项——把 ``POSTGRES_APP_USER`` 改成 postgres 就能绕过
    # 全部按域授权。角色名因此写死在代码里，配置里不再存在这个开关。
    #
    # 口令仍可配置（生产由密钥系统注入）；RLS 生效前提是该角色为
    # NOSUPERUSER / NOBYPASSRLS，由迁移 c2d3e4f5a6b7 保证。
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
    # 端点熔断（R9）：连续失败达到阈值后 OPEN，冷却期满半开探测。
    # 与事件级退避+死信分层：退避解决「单条事件重试」，熔断解决「下游整体
    # 故障时新事件不再逐条撞墙」。0 表示关闭熔断（退化永远放行）。
    outbox_webhook_circuit_failure_threshold: int = 5
    outbox_webhook_circuit_recovery_seconds: float = 30.0

    # 生成任务执行租约（workers/generation，ADR-R34）。
    # 契约 9.4「Worker 崩溃 → 租约到期后重新领取」，12.6 的目标是「租约到期后
    # 5 分钟内重新调度」——租约时长直接决定这个目标的上界：崩溃到恢复最长
    # 约等于 lease + 一个轮询间隔。默认 300 秒与 12.6 的建议值一致；调大它
    # 等于调大崩溃恢复延迟，调小则会让「真实供应商调用」在执行中被误判为
    # 崩溃（长任务必须续租，见 ADR-R34）。
    generation_lease_seconds: float = 300.0

    # 错误码目录（GET /errors/{code}）。它是内部调试面，不是公共 API：
    # - 默认关闭（false），关闭时一律返回 NOT_FOUND，与未知错误码不可区分；
    # - 关闭状态下要访问需配置 ERRORS_CATALOG_TOKEN 并带 X-Internal-Token 头；
    # - 不再用 app_env 判断：环境名写错就把内部面暴露出去，这个开关必须显式。
    errors_catalog_public: bool = False
    errors_catalog_token: SecretStr = SecretStr("")

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
    # 进程角色（B4）：api 只验签、issuer 只签发。默认 api。
    # 这个字段存在的意义是让"私钥出现在 API 进程"成为一个**可判定的错误**，
    # 而不是一个"部署时大家记得别这么干"的约定。
    identity_process_role: Literal["api", "issuer"] = "api"
    # 私钥 PEM（签发用）：只允许 issuer 进程配置。API 进程配置即拒绝启动。
    identity_private_key_pem: SecretStr = SecretStr("")
    # 公钥 PEM（验签用，可安全分发）：API 进程二选一。
    identity_public_key_pem: SecretStr = SecretStr("")
    # JWKS 文档路径（验签用）：公钥的另一种分发形态，支持多个 kid（密钥轮换）。
    identity_jwks_file: str = ""
    # JWKS 热轮换轮询间隔（秒）：>0 且验签公钥来自 JWKS 文件（而非 PEM 环境变量）
    # 时，API 进程周期性重读文件，文档变化即整体替换验签公钥（ADR-R29）。
    # 默认 0 = 关闭：PEM 来源的轮换本来伴随进程重启，热刷新只对文件来源有意义。
    identity_jwks_refresh_seconds: int = 0

    # 可观测
    otel_exporter_otlp_endpoint: str = ""
    otel_exporter_otlp_insecure: bool = False
    otel_service_name: str = "ai-film-industrial-platform"

    # 生产环境强制非空的敏感字段（密钥不落日志，且严禁空密钥误上线）。
    # 约定：这些字段在 production 下必须显式配置真实值，否则拒绝启动。
    #
    # P3-6：这里只列**真有实现**的依赖。此前把 s3_access_key_id /
    # s3_secret_access_key / mq_url / model_gateway_api_key 也列为必需，
    # 但全仓不存在任何 S3/MQ/模型网关实现——强制配置等于逼迫运维填假值，
    # 而"填了假值就能过"的检查不构成保护。等对应适配器落地时再把它们
    # 连同实现一起加回（契约先登记、再实现）。
    # B4 修正：``identity_private_key_pem`` 已从必填项中**移除**。它此前被列为
    # 生产必填，等于强制每个 API 实例都持有签发私钥——这不是"配置疏漏"，而是
    # 把最危险的部署形态写成了合规要求。现在私钥只在 issuer 进程出现，
    # 由 ``identity_process_role`` 与本校验共同约束。
    _PRODUCTION_REQUIRED_SECRETS: tuple[tuple[str, str], ...] = (
        ("postgres_password", "POSTGRES_PASSWORD"),
        ("postgres_app_password", "POSTGRES_APP_PASSWORD"),
    )

    @model_validator(mode="after")
    def _enforce_identity_process_role(self) -> Settings:
        """进程角色与密钥必须自洽：API 无签发私钥，issuer 必有私钥。

        这条校验与 ``packages/identity/guards.py`` 的启动守卫是**两道**，
        不是重复：本校验在任何进程构造 Settings 时生效（包括 worker、脚本），
        守卫在 API 入口再做一次并给出"该怎么修"的信息。少任何一道，
        都会留下"某个入口没走校验"的可能。
        """
        has_private = bool(self.identity_private_key_pem.get_secret_value().strip())

        if self.identity_process_role == "api":
            if has_private:
                raise ValueError(
                    "API 进程（IDENTITY_PROCESS_ROLE=api）不得配置签发私钥 "
                    "IDENTITY_PRIVATE_KEY_PEM：请移除私钥挂载，改用 "
                    "IDENTITY_PUBLIC_KEY_PEM 或 IDENTITY_JWKS_FILE。"
                )
        elif not has_private:  # issuer
            raise ValueError(
                "签发进程（IDENTITY_PROCESS_ROLE=issuer）必须配置 "
                "IDENTITY_PRIVATE_KEY_PEM。"
            )

        # 「公钥必须存在」不在这里校验，而在 ``get_public_key_store()`` 里：
        # Settings 也被迁移脚本、worker、CI 使用，它们不需要验签能力；
        # 在这里要求公钥会让 alembic / 后台任务因为一个无关字段起不来，
        # 逼着大家在每个场景填一个假值——那是"填了就能过"的假检查。
        return self

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
