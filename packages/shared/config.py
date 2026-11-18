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
    # 限流后端熔断（压测实测暴露：Redis 不可达时每请求固定阻塞一个
    # socket_connect_timeout 才 fail-open，降级比不降级更糟）。
    # 连续失败达阈值后 OPEN，冷却期满半开探测；0 表示关闭熔断
    # （与 webhook 端点熔断同一口径：用 0 表达关闭，不另加 enabled 开关）。
    # 默认 3 而非 5：同机/VPC 的 Redis 连续 3 次失败基本可判定后端已死，
    # 每多一次阈值就多付一个完整连接超时。
    rate_limit_circuit_failure_threshold: int = 3
    rate_limit_circuit_recovery_seconds: float = 30.0

    # 就绪探针（GET /readyz）。探针要回答「现在能不能接流量」，每次真探测
    # 两个域连接池约 90ms；编排系统按秒级轮询时这会变成可观负载，且并发探测
    # 会与业务抢连接。缓存 TTL 让探测频率与请求频率解耦。
    # 失败结果**同样缓存**：数据库挂掉时探测成本最高（要等超时），不缓存
    # 会让探针成为把连接池彻底打满的最后一根稻草。
    readiness_cache_ttl_seconds: float = 1.0
    readiness_timeout_seconds: float = 2.0

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
    # 加载 PEM（生产应从密钥管理服务注入，见 services/identity_issuance）。
    identity_issuer: str = "ai-film-identity"
    identity_audience: str = "ai-film-api"
    identity_access_token_ttl_seconds: int = 900
    # 私钥 PEM（签发用，仅身份服务持有；API 层不接触）。生产环境必填。
    identity_private_key_pem: SecretStr = SecretStr("")
    # 公钥 PEM（验签用，可安全分发）。生产环境必填。
    identity_public_key_pem: SecretStr = SecretStr("")
    # 身份服务进程监听地址（workers/identity 的 JWKS 端点）。
    # 与 API 分开端口：同镜像不同 entrypoint，避免与业务面抢 8000。
    identity_host: str = "0.0.0.0"
    identity_port: int = 8100

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
        # KEYS-3：identity 签发方是独立进程，只持私钥并对外提供 JWKS——
        # 它不连接数据库（workers/identity 零 DB import），因此不要求任何
        # postgres 口令；app_role 仅作为遗留兼容路径由测试/工具显式使用。
        "identity": (
            ("identity_private_key_pem", "IDENTITY_PRIVATE_KEY_PEM"),
            ("identity_public_key_pem", "IDENTITY_PUBLIC_KEY_PEM"),
        ),
    }

    def validate_process_requirements(self) -> None:
        """校验「本进程在生产环境必需的密钥」，缺失即拒绝启动（fail-closed）。

        为什么是**显式方法**而不是 ``model_validator``（F05/KEYS-1 根因修正）：
        构造 Settings 只是「读配置」这一件事，它并不代表「本进程宣告了身份」。
        把生产密钥校验塞进 model_validator，会让**任何**读取配置的地方（DSN
        构造、分区护栏、alembic env）都隐式承担 api 进程的密钥要求——relay
        进程构造自己的 DSN 时，只因内部调了一次配置读取，就被要求持有 API 的
        身份公钥，于是启动失败。这是「配置读取」与「进程身份校验」耦合造成的
        跨进程越界（审查 P1-04 / R7 的根因）。

        现在职责分开：
        - ``Settings()`` / ``get_settings()`` = 纯配置读取，无进程语义；
        - 进程入口经 ``get_process_settings(role)`` 声明身份，本方法在那一刻
          按该角色的清单校验，缺密钥仍拒绝启动（fail-closed 不变）。

        由 ``tests/unit/test_process_entrypoints.py`` 保证每个进程入口都走
        ``get_process_settings``，不会因为「忘了校验」而静默放行。
        """
        if self.app_env != "production":
            return
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
        # 显式拒绝开发默认值：各角色口令若取自仓库里的开发默认值，生产环境
        # 的数据库角色口令即「可预测」，RLS 边界形同虚设。清单收敛为模块级
        # DEVELOPMENT_DEFAULT_PASSWORDS（单一事实源）；按进程角色判断——只有
        # 真正使用该角色的进程才校验其默认口令，避免 outbox_maintenance（走
        # postgres 超级用户）被无关角色的密码牵制。**迁移路径不声明进程身份、
        # 不走本方法**：建角色的迁移必须显式调用 reject_development_default_password。
        for field_name, env_name in required:
            default = DEVELOPMENT_DEFAULT_PASSWORDS.get(field_name)
            if default and getattr(self, field_name).get_secret_value() == default:
                raise ValueError(
                    "生产环境（APP_ENV=production）不得使用开发默认数据库密码"
                    f" {default}（{env_name}），请由部署系统注入独立密码。"
                )

    @model_validator(mode="after")
    def _require_distinct_database_roles(self) -> Settings:
        """业务域角色名必须互不相同（P2-02：架构边界不能靠配置巧合成立）。

        三个角色名都可通过环境变量改：若部署时把 project_planning 与
        asset_service 配成同一个名字，DATA-1 的「模块级角色隔离」会在配置层
        静默失效——迁移照常建角色、服务照常启动、CI 全绿，实际上两个业务域
        共用一套权限。这种失效无法在运行期被发现（权限看起来「都通」），
        只能在启动期拒绝。

        与进程身份无关（任何读配置的地方都该看到同一个约束），故放在构造期。
        """
        roles = {
            "POSTGRES_PROJECT_PLANNING_USER": self.postgres_project_planning_user,
            "POSTGRES_ASSET_SERVICE_USER": self.postgres_asset_service_user,
            "POSTGRES_RELAY_USER": self.postgres_relay_user,
            "POSTGRES_APP_USER": self.postgres_app_user,
        }
        seen: dict[str, str] = {}
        for env_name, role in roles.items():
            if role in seen:
                raise ValueError(
                    f"数据库角色名重复：{seen[role]} 与 {env_name} 都是 {role!r}。"
                    "域角色必须互不相同，否则模块级角色隔离（DATA-1）在配置层失效。"
                )
            seen[role] = env_name
        return self

    @model_validator(mode="after")
    def _require_sane_resilience_params(self) -> Settings:
        """熔断与探针参数必须自洽（开着保护设施却给了不可能生效的参数）。

        这类配置错误的特点是**不报错地失效**：阈值写 0 以为「更保守」，
        实际是「永不熔断」；TTL 写 0 以为「总是最新」，实际是「每请求都真打
        数据库」。启动期拒绝，比上线后靠压测发现便宜得多。
        """
        circuits = (
            (
                "限流后端",
                self.rate_limit_circuit_failure_threshold,
                self.rate_limit_circuit_recovery_seconds,
            ),
            (
                "webhook 端点",
                self.outbox_webhook_circuit_failure_threshold,
                self.outbox_webhook_circuit_recovery_seconds,
            ),
        )
        for name, threshold, recovery in circuits:
            if threshold < 0:
                raise ValueError(
                    f"{name}熔断阈值不能为负（0 表示关闭熔断）：{threshold}"
                )
            if threshold > 0 and recovery <= 0:
                raise ValueError(
                    f"{name}熔断已开启（阈值 {threshold}）但冷却期 <= 0：{recovery}"
                )
        if self.readiness_cache_ttl_seconds < 0:
            raise ValueError(
                "READINESS_CACHE_TTL_SECONDS 不能为负（0 表示不缓存）："
                f"{self.readiness_cache_ttl_seconds}"
            )
        if self.readiness_timeout_seconds <= 0:
            raise ValueError(
                f"READINESS_TIMEOUT_SECONDS 必须 > 0：{self.readiness_timeout_seconds}"
            )
        return self

    @model_validator(mode="after")
    def _require_webhook_secret_with_url(self) -> Settings:
        """配了 webhook URL 却不配密钥时拒绝启动（所有环境，不只生产）。

        理由：无签名的外发端点无法被消费方区分「平台发的」与「别人伪造的」，
        这类配置错误一旦上线就是安全隐患，因此在启动期直接失败，而不是留在
        运行期靠人发现。

        注意：这条校验**与进程身份无关**（任何进程只要配了 URL 就必须配密钥），
        因此保留在构造期，不随 ``validate_process_requirements`` 一起外移。
        """
        if self.outbox_webhook_url and not self.outbox_webhook_secret.get_secret_value():
            raise ValueError(
                "配置了 OUTBOX_WEBHOOK_URL 但缺少 OUTBOX_WEBHOOK_SECRET："
                "拒绝启动未经签名的事件外发。"
            )
        return self


# 开发默认数据库口令清单（**单一事实源**）：进程校验与建角色迁移共用。
# 这些口令以明文入库（供本地/测试开箱即用）；若带入生产，RLS 应用角色 /
# Relay 角色 / 域角色的口令即「可预测」，角色隔离形同虚设。因此两层都拒绝：
# - 进程层：``Settings.validate_process_requirements``（按角色清单命中时）；
# - 迁移层：建角色的迁移显式调用 :func:`reject_development_default_password`
#   （迁移经裸 ``get_settings()`` 读配置、不声明进程身份，进程层不会触发）。
DEVELOPMENT_DEFAULT_PASSWORDS: dict[str, str] = {
    "postgres_app_password": "app_dev_password",
    "postgres_relay_password": "relay_dev_password",
    "postgres_project_planning_password": "project_planning_dev_password",
    "postgres_asset_service_password": "asset_service_dev_password",
}


def reject_development_default_password(
    settings: Settings, field_name: str, env_name: str
) -> None:
    """生产环境拒绝以开发默认口令创建/同步数据库角色（迁移层守卫）。

    建角色的迁移（9e3b4c5d6e7f / 7a8b9c0d1e2f / 8f3b2c1d4e5a）经裸
    ``get_settings()`` 读取配置，不声明进程身份，进程层校验不会触发；
    必须在取口令处显式调用本函数（fail-closed）。仅拒绝「值 == 开发默认」，
    不要求非空——空值由迁移自身的缺省拒绝兜底。
    """
    if settings.app_env != "production":
        return
    default = DEVELOPMENT_DEFAULT_PASSWORDS.get(field_name)
    if default and getattr(settings, field_name).get_secret_value() == default:
        raise RuntimeError(
            "生产环境（APP_ENV=production）不得使用开发默认数据库密码"
            f" {default}（{env_name}），请由部署系统注入独立密码。"
        )


@lru_cache
def get_settings() -> Settings:
    """获取配置单例（**纯配置读取**，不承担任何进程身份语义）。

    与 ``get_process_settings`` 的分工（审查 P1-04 根因）：
    - 本函数只负责把环境变量读成 Settings，供 DSN 构造、分区护栏、alembic
      env 等**基础设施代码**使用。它不做生产密钥校验——否则每个读配置的地方
      都会被迫持有 api 进程的密钥，正是 relay/运维进程「启动即失败」的结构性
      来源。
    - 需要「本进程缺密钥就拒绝启动」的**进程入口**必须用
      ``get_process_settings(role)``，那条路径才会执行
      ``validate_process_requirements()``。
    """
    return Settings()


@lru_cache
def get_process_settings(process_role: ProcessRole) -> Settings:
    """按进程角色获取配置单例，并**在该进程边界**执行生产密钥校验。

    为什么不用全局 ``get_settings()`` 加环境变量兜底：进程角色是**进程身份的
    一部分**，不该依赖部署方「记得设 PROCESS_ROLE」才能正确——那正是 F05 债务
    的结构性来源（配置对进程身份无知）。由进程入口显式调用本函数，把「我是谁」
    从环境变量猜测提升为代码契约：relay 入口必然以 relay 角色构造配置，
    outbox_maintenance 必然以 outbox_maintenance 角色构造，杜绝默认值误套。

    fail-closed 的位置也一并说清：构造完成后立刻按该角色的必需密钥清单校验，
    缺失即 ``ValueError``（拒绝启动）。校验发生在**声明身份的那一刻**，而不是
    每次读配置的时刻——后者会让基础设施代码替业务进程背密钥要求。
    """
    settings = Settings(process_role=process_role)
    settings.validate_process_requirements()
    return settings
