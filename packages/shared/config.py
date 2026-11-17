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

# 进程角色（F05 进程密钥最小权限）：一个部署单元（进程）声明自己在平台中的
# 角色，据此只校验「该进程真正需要的密钥」。这是把进程职责差异显式建模为
# 配置维度，而非继续用一份进程无关的全局清单去迁就（否则 API/Relay 也会被
# 强迫持有身份签发私钥，与 auth_dependency 只用公钥验签自相矛盾）。
#   api                —— FastAPI 服务进程：持公钥验签，不持签发私钥。
#   relay              —— Outbox 投递进程：完全不碰身份密钥。
#   outbox_maintenance —— Outbox 运维 CLI：用 postgres 超级用户做 DDL。
#   identity           —— 身份签发服务：持私钥（+公钥）签发令牌。
ProcessRole = Literal["api", "relay", "outbox_maintenance", "identity"]


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
    # 当前进程的角色（决定生产启动时校验哪一份密钥清单）。默认 api：既有
    # 部署以 API 为主，未显式声明时按 API 语义处理，避免把签发私钥误要求到
    # 每个进程上。
    process_role: ProcessRole = "api"

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
    # Outbox Relay 专用角色（F03 数据所有权：进程边界隔离）。Relay 只消费
    # outbox 事件并回写投递状态，不得持有任何业务 schema 的写权限；以独立
    # 角色连接，使「Relay 越界写业务表」在数据库层被拒绝，而非依赖代码自觉。
    postgres_relay_user: str = "outbox_relay_role"
    postgres_relay_password: SecretStr = SecretStr("relay_dev_password")
    # 业务域角色（DATA-1 模块级角色隔离）。project_planning 与 asset_service
    # 各自独立角色 + 独立连接池，业务 schema 写权限严格隔离；共享基础设施
    # （outbox 写 / auth 读）两角色按需持有。迁移 8f3b2c1d4e5a 创建这些角色。
    postgres_project_planning_user: str = "project_planning_role"
    postgres_project_planning_password: SecretStr = SecretStr("project_planning_dev_password")
    postgres_asset_service_user: str = "asset_service_role"
    postgres_asset_service_password: SecretStr = SecretStr("asset_service_dev_password")

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
    #
    # P3-6：这里只列**真有实现**的依赖。此前把 s3_access_key_id /
    # s3_secret_access_key / mq_url / model_gateway_api_key 也列为必需，
    # 但全仓不存在任何 S3/MQ/模型网关实现——强制配置等于逼迫运维填假值，
    # 而"填了假值就能过"的检查不构成保护。等对应适配器落地时再把它们
    # 连同实现一起加回（契约先登记、再实现）。
    #
    # F05（进程密钥最小权限）：清单按 process_role 分列，不再全局一刀切。
    # 每个进程只校验自己真正需要的密钥——API 只持公钥验签（不持签发私钥）、
    # Relay 完全不碰身份密钥、运维 CLI 用 postgres 超级用户做 DDL、只有
    # identity 签发方持私钥。新增进程/密钥时在矩阵里加一行/一列即可，而不是
    # 继续在单一清单上堆字段（那正是 KEYS-1/KEYS-2 债务的结构性来源）。
    _PRODUCTION_REQUIRED_SECRETS_BY_PROCESS: dict[ProcessRole, tuple[tuple[str, str], ...]] = {
        "api": (
            ("postgres_project_planning_password", "POSTGRES_PROJECT_PLANNING_PASSWORD"),
            ("postgres_asset_service_password", "POSTGRES_ASSET_SERVICE_PASSWORD"),
            ("identity_public_key_pem", "IDENTITY_PUBLIC_KEY_PEM"),
        ),
        "relay": (
            ("postgres_relay_password", "POSTGRES_RELAY_PASSWORD"),
        ),
        "outbox_maintenance": (
            ("postgres_password", "POSTGRES_PASSWORD"),
        ),
        "identity": (
            ("postgres_app_password", "POSTGRES_APP_PASSWORD"),
            ("identity_private_key_pem", "IDENTITY_PRIVATE_KEY_PEM"),
            ("identity_public_key_pem", "IDENTITY_PUBLIC_KEY_PEM"),
        ),
    }

    @model_validator(mode="after")
    def _reject_empty_secrets_in_production(self) -> Settings:
        """生产环境（app_env=production）下，本进程必需密钥为空时拒绝启动。

        按 process_role 取对应清单，仅拦截「完全未配置」的空密钥
        （SecretStr 值为空字符串），避免把本地开发默认值带入生产造成事故。
        """
        if self.app_env != "production":
            return self
        required = self._PRODUCTION_REQUIRED_SECRETS_BY_PROCESS[self.process_role]
        missing: list[str] = []
        for field_name, env_name in required:
            value = getattr(self, field_name)
            secret = value.get_secret_value() if isinstance(value, SecretStr) else value
            if not secret:
                missing.append(env_name)
        if missing:
            raise ValueError(
                f"生产环境（APP_ENV=production，PROCESS_ROLE={self.process_role}）"
                "缺少必需密钥，拒绝启动：" + ", ".join(missing)
            )
        # 显式拒绝开发默认值：迁移 9e3b4c5d6e7f 创建的 app_dev_password /
        # 7a8b9c0d1e2f 创建的 relay_dev_password 只允许本地/测试使用，不得带入
        # 生产（否则 RLS 应用角色/Relay 角色的口令可预测）。与必需清单一致，按
        # 进程角色判断：只有真正使用该角色的进程才校验其默认口令，避免
        # outbox_maintenance（走 postgres 超级用户）被无关的 app/relay 密码牵制。
        if "postgres_app_password" in {f for f, _ in required} and (
            self.postgres_app_password.get_secret_value() == "app_dev_password"
        ):
            raise ValueError(
                "生产环境（APP_ENV=production）不得使用开发默认应用数据库密码"
                " app_dev_password（POSTGRES_APP_PASSWORD），请由部署系统注入独立密码。"
            )
        if "postgres_relay_password" in {f for f, _ in required} and (
            self.postgres_relay_password.get_secret_value() == "relay_dev_password"
        ):
            raise ValueError(
                "生产环境（APP_ENV=production）不得使用开发默认 relay 数据库密码"
                " relay_dev_password（POSTGRES_RELAY_PASSWORD），请由部署系统注入独立密码。"
            )
        if "postgres_project_planning_password" in {f for f, _ in required} and (
            self.postgres_project_planning_password.get_secret_value()
            == "project_planning_dev_password"
        ):
            raise ValueError(
                "生产环境（APP_ENV=production）不得使用开发默认 project_planning"
                " 域数据库密码 project_planning_dev_password"
                "（POSTGRES_PROJECT_PLANNING_PASSWORD），请由部署系统注入独立密码。"
            )
        if "postgres_asset_service_password" in {f for f, _ in required} and (
            self.postgres_asset_service_password.get_secret_value()
            == "asset_service_dev_password"
        ):
            raise ValueError(
                "生产环境（APP_ENV=production）不得使用开发默认 asset_service"
                " 域数据库密码 asset_service_dev_password"
                "（POSTGRES_ASSET_SERVICE_PASSWORD），请由部署系统注入独立密码。"
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
    """获取全局配置单例（默认进程角色 api，见 Settings.process_role 文档）。"""
    return Settings()


@lru_cache
def get_process_settings(process_role: ProcessRole) -> Settings:
    """按进程角色获取配置单例（进程入口显式声明自身角色）。

    为什么不用全局 ``get_settings()`` 加环境变量兜底：进程角色是**进程身份的
    一部分**，不该依赖部署方「记得设 PROCESS_ROLE」才能正确——那正是 F05 债务
    的结构性来源（配置对进程身份无知）。由进程入口显式调用本函数，把「我是谁」
    从环境变量猜测提升为代码契约：relay 入口必然以 relay 角色构造配置，
    outbox_maintenance 必然以 outbox_maintenance 角色构造，杜绝默认值误套。

    process_role 仍可通过构造 Settings 时传 ``process_role=...`` 显式覆盖，
    但生产校验清单据此收紧到该进程真正需要的密钥。
    """
    return Settings(process_role=process_role)
