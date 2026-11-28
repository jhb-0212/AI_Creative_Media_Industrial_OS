"""packages/shared/config 密钥运行时校验的单元测试（红线 6：密钥不落日志）。

覆盖：
- 生产环境（APP_ENV=production）缺关键密钥 → 拒绝启动（进程入口校验抛 ValueError）。
- 非生产环境（development）允许空密钥，便于本地联调。
- 密钥字段为 SecretStr，repr/str 不含明文。
- F05 进程密钥最小权限：按 process_role 只校验该进程真正需要的密钥——
  API 不要求签发私钥，Relay 不碰身份密钥，运维 CLI 只用 postgres 密码。
"""

from __future__ import annotations

import pytest

from packages.shared.config import Settings

# 各进程角色在生产环境下各自「必需」的密钥清单（与
# Settings._PRODUCTION_REQUIRED_SECRETS_BY_PROCESS 逐项对齐）。这是 F05 根治的
# 断言目标：密钥需求随进程角色收紧，而非全局一刀切。
# DATA-1（按域拆角色）后，api 进程需两个业务域角色的密码，不再需要单一
# app_role 的 app_password；R16 三域拆分后 api 以五个域角色 + 一个运维角色连接，
# 六个口令均须显式提供（D6-9 加入 outbox_ops）。
_REQUIRED_BY_PROCESS = {
    "api": [
        "POSTGRES_PROJECT_PLANNING_PASSWORD",
        "POSTGRES_ASSET_SERVICE_PASSWORD",
        "POSTGRES_PRODUCTION_PLANNING_PASSWORD",
        "POSTGRES_BUDGET_QUOTA_PASSWORD",
        "POSTGRES_QC_GOVERNANCE_PASSWORD",
        "POSTGRES_OUTBOX_OPS_PASSWORD",
        "IDENTITY_PUBLIC_KEY_PEM",
    ],
    "relay": ["POSTGRES_RELAY_PASSWORD"],
    "outbox_maintenance": ["POSTGRES_PASSWORD"],
    "identity": [
        "IDENTITY_PRIVATE_KEY_PEM",
        "IDENTITY_PUBLIC_KEY_PEM",
    ],
}

# 一份「所有进程都配齐」的环境变量（密钥均为非空），供各用例取子集。
_ALL_ENV_VARS = {
    "APP_ENV": "production",
    "POSTGRES_PASSWORD": "s3cret-db",
    "POSTGRES_APP_PASSWORD": "prod-app-pass",
    "POSTGRES_RELAY_PASSWORD": "prod-relay-pass",
    "POSTGRES_PROJECT_PLANNING_PASSWORD": "prod-project-planning-pass",
    "POSTGRES_ASSET_SERVICE_PASSWORD": "prod-asset-service-pass",
    "POSTGRES_PRODUCTION_PLANNING_PASSWORD": "prod-production-planning-pass",
    "POSTGRES_BUDGET_QUOTA_PASSWORD": "prod-budget-quota-pass",
    "POSTGRES_QC_GOVERNANCE_PASSWORD": "prod-qc-governance-pass",
    "POSTGRES_OUTBOX_OPS_PASSWORD": "prod-outbox-ops-pass",
    "IDENTITY_PRIVATE_KEY_PEM": "-----BEGIN PRIVATE KEY-----prod-----END PRIVATE KEY-----",
    "IDENTITY_PUBLIC_KEY_PEM": "-----BEGIN PUBLIC KEY-----prod-----END PUBLIC KEY-----",
    "S3_ACCESS_KEY_ID": "AKIAEXAMPLE",
    "S3_SECRET_ACCESS_KEY": "s3cret-s3",
    "MQ_URL": "amqp://user:pass@mq:5672/",
    "MODEL_GATEWAY_API_KEY": "sk-model",
}


def _prod_env() -> dict[str, str]:
    """构造一份生产环境所需的完整环境变量（密钥均为非空）。"""
    return dict(_ALL_ENV_VARS)


def _make_settings(**overrides: str) -> Settings:
    """构造 Settings 并执行**进程边界**的生产密钥校验（返回已校验实例）。

    测试环境可能残留本地 .env（含占位值），若不屏蔽会污染用例：
    monkeypatch.delenv 之后 .env 仍会补回 postgres 等默认值，
    导致「缺失密钥应拒绝」的断言失效。这里强制 _env_file=None，
    使配置只来源于进程环境变量，保证用例确定性。

    P1-04 之后生产校验从 model_validator 移到显式方法
    ``validate_process_requirements``（构造 ≠ 声明进程身份）。凡本文件里
    「生产缺密钥必须拒绝」的用例，都必须走**进程入口同一条路径**：
    ``get_process_settings`` 也是「构造 + 校验」，此处对齐它。
    """
    settings = Settings(_env_file=None, **overrides)
    settings.validate_process_requirements()
    return settings


def _set_env(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    """清空既有密钥环境变量后，按给定字典注入，避免残留真实值干扰。"""
    for var in _ALL_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)


def test_production_with_all_secrets_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    """生产环境密钥齐全 → 正常实例化。"""
    _set_env(monkeypatch, _prod_env())
    settings = _make_settings()
    assert settings.app_env == "production"
    assert settings.postgres_password.get_secret_value() == "s3cret-db"


@pytest.mark.parametrize("process_role", ["api", "relay", "outbox_maintenance", "identity"])
def test_production_missing_secret_rejected_per_process(
    monkeypatch: pytest.MonkeyPatch, process_role: str
) -> None:
    """生产环境缺少**本进程必需**密钥 → 拒绝启动，并指明缺失变量名。

    每个进程取自身清单的首个必需密钥删掉，断言拒绝且报错含该变量名；
    只缺「该进程不需要的密钥」不得拒绝（见 test_production_ignores_non_required_secret）。
    """
    missing_var = _REQUIRED_BY_PROCESS[process_role][0]
    env = _prod_env()
    env.pop(missing_var)
    _set_env(monkeypatch, env)

    with pytest.raises(ValueError) as exc_info:
        _make_settings(process_role=process_role)
    assert missing_var in str(exc_info.value)


@pytest.mark.parametrize(
    "process_role,not_required",
    [
        # API 不要求签发私钥（F05 核心：私钥仅身份服务持有）。
        ("api", "IDENTITY_PRIVATE_KEY_PEM"),
        ("api", "POSTGRES_RELAY_PASSWORD"),
        ("api", "POSTGRES_PASSWORD"),
        # DATA-1（按域拆角色）：api 不再用单一 app_role，不要求 app_password。
        ("api", "POSTGRES_APP_PASSWORD"),
        # Relay 完全不碰身份密钥与业务应用角色。
        ("relay", "IDENTITY_PRIVATE_KEY_PEM"),
        ("relay", "IDENTITY_PUBLIC_KEY_PEM"),
        ("relay", "POSTGRES_APP_PASSWORD"),
        # 运维连接池只属于 API 进程：relay 与运维 CLI 都不该被它牵制。
        ("relay", "POSTGRES_OUTBOX_OPS_PASSWORD"),
        ("outbox_maintenance", "POSTGRES_OUTBOX_OPS_PASSWORD"),
        # 运维 CLI 只用 postgres 超级用户，不要求应用/relay/身份密钥。
        ("outbox_maintenance", "POSTGRES_APP_PASSWORD"),
        ("outbox_maintenance", "POSTGRES_RELAY_PASSWORD"),
        ("outbox_maintenance", "IDENTITY_PRIVATE_KEY_PEM"),
        ("outbox_maintenance", "IDENTITY_PUBLIC_KEY_PEM"),
    ],
)
def test_production_ignores_non_required_secret(
    monkeypatch: pytest.MonkeyPatch, process_role: str, not_required: str
) -> None:
    """生产环境下，进程缺少「自身不需要的密钥」不得拒绝启动。

    这是 KEYS-1/KEYS-2 根治的负向断言：若还按全局清单校验，API 缺私钥就会被
    误拒。此处逐一验证每个进程只认自己清单里的密钥，其它密钥缺失无感。
    """
    env = _prod_env()
    env.pop(not_required)
    _set_env(monkeypatch, env)

    settings = _make_settings(process_role=process_role)  # 不应抛错
    assert settings.app_env == "production"


def test_development_allows_empty_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    """非生产环境允许空密钥，不阻断本地联调。"""
    monkeypatch.setenv("APP_ENV", "development")
    for var in _ALL_ENV_VARS:
        if var != "APP_ENV":
            monkeypatch.delenv(var, raising=False)
    settings = _make_settings()
    assert settings.app_env == "development"
    assert settings.postgres_password.get_secret_value() == ""


def test_secret_fields_do_not_leak_in_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    """SecretStr 字段在 repr/str 中不泄露明文（红线 6）。"""
    _set_env(monkeypatch, _prod_env())
    settings = _make_settings()
    rendered = repr(settings)
    assert "s3cret-db" not in rendered
    assert "s3cret-s3" not in rendered


def test_identity_process_does_not_require_database_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """identity 进程不再要求任何 POSTGRES 口令（Codex P2 / KEYS-3 收口）。

    workers/identity 只持私钥并对外提供 JWKS，**零数据库 import**——历史
    清单要求 POSTGRES_APP_PASSWORD 会让生产签发方在无 DB 口令时拒绝启动，
    属于「必需清单与进程真实依赖不符」。app_role 仅是遗留兼容路径（测试/
    工具显式使用），不再出现在任何进程的必需清单里。
    """
    _set_env(monkeypatch, _prod_env())
    # 显式把 POSTGRES_APP_PASSWORD 设回开发默认值：identity 仍必须能启动——
    # 它根本不使用该口令；开发默认值的拒绝只对「真正使用该角色」的进程生效。
    monkeypatch.setenv("POSTGRES_APP_PASSWORD", "app_dev_password")
    settings = _make_settings(process_role="identity")
    assert settings.identity_private_key_pem.get_secret_value() != ""
    assert "postgres_app_password" not in {
        f for f, _ in settings._PRODUCTION_REQUIRED_SECRETS_BY_PROCESS["identity"]
    }


def test_production_rejects_dev_default_domain_passwords(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """api 进程显式使用开发默认业务域密码 → 拒绝启动。

    DATA-1（按域拆角色）后，project_planning_role / asset_service_role 的
    开发默认口令（project_planning_dev_password / asset_service_dev_password）
    不得带入生产（否则业务域角色的口令可预测）。
    """
    _set_env(monkeypatch, _prod_env())
    monkeypatch.setenv("POSTGRES_PROJECT_PLANNING_PASSWORD", "project_planning_dev_password")
    with pytest.raises(ValueError) as exc_info:
        _make_settings(process_role="api")
    assert "project_planning_dev_password" in str(exc_info.value)

    _set_env(monkeypatch, _prod_env())
    monkeypatch.setenv("POSTGRES_ASSET_SERVICE_PASSWORD", "asset_service_dev_password")
    with pytest.raises(ValueError) as exc_info:
        _make_settings(process_role="api")
    assert "asset_service_dev_password" in str(exc_info.value)


def test_production_rejects_dev_default_outbox_ops_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """api 进程显式使用运维角色开发默认口令 → 拒绝启动（D6-9）。

    运维角色能修改事件投递状态（清死信标记、交回正常 claim），是比业务域角色
    更敏感的凭证：若生产沿用仓库里的开发默认口令，任何拿到源码的人都能重放死信。
    因此它和业务域口令同口径 fail-closed。
    """
    _set_env(monkeypatch, _prod_env())
    monkeypatch.setenv("POSTGRES_OUTBOX_OPS_PASSWORD", "outbox_ops_dev_password")
    with pytest.raises(ValueError) as exc_info:
        _make_settings(process_role="api")
    assert "outbox_ops_dev_password" in str(exc_info.value)


def test_production_rejects_dev_default_relay_password(monkeypatch: pytest.MonkeyPatch) -> None:
    """生产环境 relay 进程显式使用 relay_dev_password → 拒绝启动。"""
    _set_env(monkeypatch, _prod_env())
    monkeypatch.setenv("POSTGRES_RELAY_PASSWORD", "relay_dev_password")
    with pytest.raises(ValueError) as exc_info:
        _make_settings(process_role="relay")
    assert "relay_dev_password" in str(exc_info.value)


def test_relay_dev_password_not_checked_for_non_relay_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """relay 默认口令只对 relay 进程校验：API 进程用了 relay_dev_password 也无感。

    F05 根治的补充断言：dev 默认口令检查与必需清单一致，按进程收紧，避免
    outbox_maintenance（根本不连 relay 角色）被无关的 relay 密码牵制。
    """
    _set_env(monkeypatch, _prod_env())
    monkeypatch.setenv("POSTGRES_RELAY_PASSWORD", "relay_dev_password")
    settings = _make_settings(process_role="api")  # 不应因 relay 口令拒绝
    assert settings.process_role == "api"


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
    _set_env(monkeypatch, env)

    settings = _make_settings()
    assert settings.app_env == "production"


def test_bare_construction_has_no_process_semantics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """纯配置读取（Settings()）在生产缺密钥时**不**报错（P1-04 根因修正）。

    构造 Settings 只代表「读配置」，不代表「本进程宣告了身份」。把它和生产
    密钥校验绑定，会让任何读配置的基础设施代码（DSN 构造、分区护栏、alembic
    env）都替 api 进程背密钥要求——relay/运维进程因此「启动即失败」。

    fail-closed 并未削弱：进程入口经 get_process_settings 声明身份并校验
    （见 test_production_missing_secret_rejected_per_process）。
    """
    _set_env(monkeypatch, {"APP_ENV": "production", "POSTGRES_PASSWORD": "x"})
    settings = Settings(_env_file=None)  # 不调用 validate_process_requirements
    assert settings.app_env == "production"
    assert settings.identity_public_key_pem.get_secret_value() == ""


def test_process_settings_declares_and_validates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """get_process_settings = 声明身份 + 该校验立刻执行（fail-closed 落点）。"""
    from packages.shared.config import get_process_settings

    _set_env(monkeypatch, {"APP_ENV": "production", "POSTGRES_PASSWORD": "x"})
    # relay 缺 POSTGRES_RELAY_PASSWORD → 拒绝启动。
    with pytest.raises(ValueError) as exc_info:
        get_process_settings("relay")
    assert "POSTGRES_RELAY_PASSWORD" in str(exc_info.value)


# ---------------------------------------------------------------------------
# 迁移层守卫（Codex P1 #12/#13）：建角色迁移用裸 get_settings()，不走
# validate_process_requirements——必须显式调用 reject_development_default_password。
# ---------------------------------------------------------------------------


def test_migration_guard_rejects_dev_default_password_in_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """生产环境 + 开发默认口令 → RuntimeError（迁移路径唯一防线）。"""
    from packages.shared.config import reject_development_default_password

    _set_env(monkeypatch, _prod_env())
    monkeypatch.setenv("POSTGRES_RELAY_PASSWORD", "relay_dev_password")
    settings = Settings(_env_file=None, process_role="relay")
    with pytest.raises(RuntimeError) as exc_info:
        reject_development_default_password(
            settings, "postgres_relay_password", "POSTGRES_RELAY_PASSWORD"
        )
    assert "relay_dev_password" in str(exc_info.value)


def test_migration_guard_rejects_dev_default_domain_passwords(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """域角色口令取开发默认值 → 同样被迁移守卫拒绝。"""
    from packages.shared.config import reject_development_default_password

    for field, env_name, default in (
        (
            "postgres_project_planning_password",
            "POSTGRES_PROJECT_PLANNING_PASSWORD",
            "project_planning_dev_password",
        ),
        (
            "postgres_asset_service_password",
            "POSTGRES_ASSET_SERVICE_PASSWORD",
            "asset_service_dev_password",
        ),
        ("postgres_app_password", "POSTGRES_APP_PASSWORD", "app_dev_password"),
        ("postgres_outbox_ops_password", "POSTGRES_OUTBOX_OPS_PASSWORD", "outbox_ops_dev_password"),
    ):
        _set_env(monkeypatch, _prod_env())
        monkeypatch.setenv(env_name, default)
        settings = Settings(_env_file=None, process_role="api")
        with pytest.raises(RuntimeError, match=default):
            reject_development_default_password(settings, field, env_name)


def test_migration_guard_allows_development_default_outside_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """非生产环境不触发（本地/测试开箱即用语义不变）。"""
    from packages.shared.config import reject_development_default_password

    settings = Settings(_env_file=None, process_role="relay")  # 默认 app_env=development
    # 不应抛出：开发默认口令只在生产被拒绝。
    reject_development_default_password(
        settings, "postgres_relay_password", "POSTGRES_RELAY_PASSWORD"
    )


def test_migration_guard_allows_injected_production_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """生产环境 + 部署注入的独立口令 → 放行（不误伤正常部署）。"""
    from packages.shared.config import reject_development_default_password

    _set_env(monkeypatch, _prod_env())
    settings = Settings(_env_file=None, process_role="relay")
    reject_development_default_password(
        settings, "postgres_relay_password", "POSTGRES_RELAY_PASSWORD"
    )


def test_duplicate_domain_role_names_rejected() -> None:
    """两个域角色配成同名 → 启动期拒绝（P2-02）。

    角色名可配置，若配置成同一个，DATA-1 的模块级隔离会被一个环境变量抹掉，
    而运行期看不出异常（权限看起来都通）。因此必须在配置构造期拒绝。
    """
    from packages.shared.config import Settings

    with pytest.raises(ValueError) as exc:
        Settings(
            postgres_project_planning_user="same_role",
            postgres_asset_service_user="same_role",
        )
    assert "角色名重复" in str(exc.value)


def test_distinct_domain_role_names_accepted() -> None:
    """默认角色名互不相同 → 正常构造（不误伤）。"""
    from packages.shared.config import Settings

    settings = Settings()
    assert (
        len(
            {
                settings.postgres_project_planning_user,
                settings.postgres_asset_service_user,
                settings.postgres_relay_user,
                settings.postgres_app_user,
                # 运维角色与任何域角色同名 = 域角色获得改投递状态的能力。
                settings.postgres_outbox_ops_user,
            }
        )
        == 5
    )


# --------------------------------------------------------------------------- #
# 熔断与探针参数校验（保护设施不得静默失效）
# --------------------------------------------------------------------------- #
def test_negative_circuit_threshold_rejected() -> None:
    """负阈值拒绝：-1 既不表示开启也不表示关闭，是明确的配置错误。"""
    from packages.shared.config import Settings

    with pytest.raises(ValueError) as exc:
        Settings(rate_limit_circuit_failure_threshold=-1)
    assert "熔断阈值不能为负" in str(exc.value)


def test_enabled_circuit_with_zero_recovery_rejected() -> None:
    """开了熔断却没有冷却期 → 拒绝启动。

    这种组合会让熔断器一打开就永不半开（冷却期 =0 时行为依赖实现细节），
    结果与「永久熔断」等价，比不开更糟。
    """
    from packages.shared.config import Settings

    with pytest.raises(ValueError) as exc:
        Settings(
            rate_limit_circuit_failure_threshold=3,
            rate_limit_circuit_recovery_seconds=0,
        )
    assert "冷却期" in str(exc.value)


def test_zero_threshold_means_disabled_and_skips_recovery_check() -> None:
    """阈值 0 = 关闭熔断（与 webhook 同一口径），此时冷却期不参与判定。"""
    from packages.shared.config import Settings

    settings = Settings(
        rate_limit_circuit_failure_threshold=0,
        rate_limit_circuit_recovery_seconds=0,
    )
    assert settings.rate_limit_circuit_failure_threshold == 0


def test_invalid_readiness_bounds_rejected() -> None:
    """探针参数越界 → 拒绝启动（TTL 为负会让缓存永不过期）。"""
    from packages.shared.config import Settings

    with pytest.raises(ValueError) as exc:
        Settings(readiness_cache_ttl_seconds=-1)
    assert "READINESS_CACHE_TTL_SECONDS" in str(exc.value)

    with pytest.raises(ValueError) as exc:
        Settings(readiness_timeout_seconds=0)
    assert "READINESS_TIMEOUT_SECONDS" in str(exc.value)
