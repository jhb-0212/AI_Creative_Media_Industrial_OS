"""数据库基础设施（SQLAlchemy 异步引擎 + 会话工厂 + 声明基类）。

设计要点（P0-4 根治，ADR 3.4）：
- 引擎与会话工厂由 FastAPI Lifespan 统一管理，挂到 ``app.state``，随应用
  启动/关闭创建/释放 asyncpg 连接池；不再使用全局单例 + 事件循环动态重建。
- 事务边界由 ``packages.shared.unit_of_work`` 统一提供（API 与 Worker 同一实现），
  本模块只负责引擎/会话工厂与声明基类，不参与提交语义。
- 测试由 conftest 注入独立引擎/会话工厂，生产代码零测试感知。
- 密钥不落日志：DSN 中的密码来自 SecretStr，仅用于连接，不打印。
- 连接池与超时参数显式配置，为高并发预留（pool_size / max_overflow）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from urllib.parse import quote

from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from packages.shared.config import Settings, get_settings
from packages.shared.logging import get_logger
from packages.shared.rate_limit import RateLimiter
from packages.shared.readiness import ReadinessProbe, ReadinessResult
from packages.shared.rls import verify_rls_enforcement
from packages.telemetry.tracing import shutdown_telemetry

logger = get_logger("packages.shared.database")


class Base(DeclarativeBase):
    """统一声明基类，供各领域模块的 ORM 模型继承。"""

    pass


def _build_url(user: str, password: str, settings: Settings) -> str:
    """按「角色凭据 + 连接参数」拼装异步 DSN（唯一的拼装实现）。

    五个角色（app / migration / relay / project_planning / asset_service）的
    DSN 只差用户名与口令，此前是五份重复的 f-string——任何一处编码规则改动
    都要同步五次，漏一处就是「某个进程连不上」。收敛为一个实现（审查 P1-04
    的另一个切面：连接构造不应有多个事实源）。

    用户名与密码均做 URL 编码，避免特殊字符破坏连接串解析。
    用 ``quote(safe="")`` 而非 ``quote_plus``：userinfo 里 ``+`` 是字面字符，
    密码含空格时若编码成 ``+`` 会被当作字面加号，导致凭据解错。
    """
    return (
        f"postgresql+asyncpg://{quote(user, safe='')}:{quote(password, safe='')}"
        f"@{settings.postgres_host}:{settings.postgres_port}/{settings.postgres_db}"
    )


def _settings_or_default(settings: Settings | None) -> Settings:
    """未显式传入进程配置时退回全局配置（**纯读取**，无进程身份校验）。

    进程入口必须显式传入自己的 ``get_process_settings(role)``：DSN 是「该进程
    连库所用的角色」，与进程身份必须是同一份配置上下文（审查 P1-04 根因）。
    这里保留默认值只为测试与单域工具（不声明进程身份的调用方）能用。
    """
    return settings if settings is not None else get_settings()


def build_database_url(settings: Settings | None = None) -> str:
    """构造应用连接的异步 DSN（asyncpg 驱动，应用角色 app_role）。

    RLS 生效前提：应用以非超级用户角色（app_role）连接，否则超级用户
    BYPASSRLS 导致行级安全失效（决策 5 的终极防呆不设防）。app_role 由
    迁移 9e3b4c5d6e7f 创建，仅授权 outbox 只读与 auth 只读，无 DDL 权限。

    生产业务链接已按域拆到 project_planning_role / asset_service_role
    （见下列构造器）；本函数保留给「以单一应用角色连接」的测试与工具。
    """
    cfg = _settings_or_default(settings)
    return _build_url(
        cfg.postgres_app_user, cfg.postgres_app_password.get_secret_value(), cfg
    )


def build_migration_url(settings: Settings | None = None) -> str:
    """构造迁移（alembic）连接的异步 DSN（超级用户，需 DDL 权限）。

    迁移需要 CREATE/ALTER/DROP schema、表、角色等 DDL 权限，必须用超级
    用户（postgres）连接；应用运行时才用域角色（无 DDL 权限）。二者
    职责分离，避免应用角色拥有过高权限。

    调用方（alembic env / 运维 CLI / 运维类测试）应显式传入
    ``get_process_settings("outbox_maintenance")`` 或迁移语义的配置，使
    「连库角色」与「进程身份」一致。
    """
    cfg = _settings_or_default(settings)
    return _build_url(
        cfg.postgres_user, cfg.postgres_password.get_secret_value(), cfg
    )


def build_relay_database_url(settings: Settings | None = None) -> str:
    """构造 Outbox Relay 连接的异步 DSN（专用角色 outbox_relay_role）。

    Relay 只消费 outbox 事件并回写投递元数据列，以独立角色连接（F03 数据
    所有权的进程边界隔离）；该角色仅持有 outbox schema 的 SELECT + 列级
    UPDATE（published_at/attempts/next_attempt_at/dead_lettered_at），不持有
    任何业务 schema 的写权限，也不持有事件业务内容的 UPDATE 权限。
    """
    cfg = _settings_or_default(settings)
    return _build_url(
        cfg.postgres_relay_user, cfg.postgres_relay_password.get_secret_value(), cfg
    )


def build_project_planning_database_url(settings: Settings | None = None) -> str:
    """构造 project_planning 域连接的异步 DSN（角色 project_planning_role）。

    DATA-1（模块级角色隔离）：project_planning 域以独立角色连接，只持有
    project_planning schema 的写权限 + outbox 事件写 + auth 只读，不得写
    asset_service schema，使跨域越界写在数据库层被拒绝。
    """
    cfg = _settings_or_default(settings)
    return _build_url(
        cfg.postgres_project_planning_user,
        cfg.postgres_project_planning_password.get_secret_value(),
        cfg,
    )


def build_asset_service_database_url(settings: Settings | None = None) -> str:
    """构造 asset_service 域连接的异步 DSN（角色 asset_service_role）。

    DATA-1（模块级角色隔离）：asset_service 域以独立角色连接，只持有
    asset_service schema 的写权限 + outbox 事件写 + auth 只读，不得写
    project_planning schema，使跨域越界写在数据库层被拒绝。
    """
    cfg = _settings_or_default(settings)
    return _build_url(
        cfg.postgres_asset_service_user,
        cfg.postgres_asset_service_password.get_secret_value(),
        cfg,
    )


def build_production_planning_database_url(settings: Settings | None = None) -> str:
    """构造 production_planning 域 DSN（角色 production_planning_role）。

    该角色只持有 production_routes / generation_tasks 两张表的写权限、平台共享
    表（audit/outbox/auth）与项目域三列的只读；它**没有**账本与 Gate 决策的任何
    权限——「生产域顺手改一笔预算」在数据库层被拒绝。
    """
    cfg = _settings_or_default(settings)
    return _build_url(
        cfg.postgres_production_planning_user,
        cfg.postgres_production_planning_password.get_secret_value(),
        cfg,
    )


def build_budget_quota_database_url(settings: Settings | None = None) -> str:
    """构造 budget_quota 域 DSN（角色 budget_quota_role）。

    账本是追加式不可变事实：该角色对 budget_ledger 只有 SELECT/INSERT，物理上
    不可能 UPDATE/DELETE 一笔已落库的金额——冲正只能新增流水。
    """
    cfg = _settings_or_default(settings)
    return _build_url(
        cfg.postgres_budget_quota_user,
        cfg.postgres_budget_quota_password.get_secret_value(),
        cfg,
    )


def build_qc_governance_database_url(settings: Settings | None = None) -> str:
    """构造 qc_governance 域 DSN（角色 qc_governance_role）。

    决策记录是证据：只 SELECT/INSERT，撤销 Gate 结论要记**新**决策，不能改写
    历史（改写会让「当时凭什么放行」永久丢失）。
    """
    cfg = _settings_or_default(settings)
    return _build_url(
        cfg.postgres_qc_governance_user,
        cfg.postgres_qc_governance_password.get_secret_value(),
        cfg,
    )


def _make_engine(url: str) -> AsyncEngine:
    """按给定 DSN 创建异步引擎（连接池参数为高并发预留）。"""
    return create_async_engine(
        url,
        echo=False,
        pool_size=10,
        max_overflow=20,
        pool_pre_ping=True,
        pool_recycle=1800,
    )


def create_engine() -> AsyncEngine:
    """创建应用引擎（app_role，向后兼容：测试/单域场景仍可用）。

    生产按域拆连接池后，业务代码改用 create_project_planning_engine /
    create_asset_service_engine（见 lifespan）；本函数仅保留给 conftest 等
    需要单连接池的场景，避免破坏既有测试装配。
    """
    return _make_engine(build_database_url())


def create_project_planning_engine() -> AsyncEngine:
    """创建 project_planning 域引擎（角色 project_planning_role）。"""
    return _make_engine(build_project_planning_database_url())


def create_asset_service_engine() -> AsyncEngine:
    """创建 asset_service 域引擎（角色 asset_service_role）。"""
    return _make_engine(build_asset_service_database_url())


def create_production_planning_engine() -> AsyncEngine:
    """创建 production_planning 域引擎（角色 production_planning_role）。"""
    return _make_engine(build_production_planning_database_url())


def create_budget_quota_engine() -> AsyncEngine:
    """创建 budget_quota 域引擎（角色 budget_quota_role）。"""
    return _make_engine(build_budget_quota_database_url())


def create_qc_governance_engine() -> AsyncEngine:
    """创建 qc_governance 域引擎（角色 qc_governance_role）。"""
    return _make_engine(build_qc_governance_database_url())


def create_relay_engine(settings: Settings | None = None) -> AsyncEngine:
    """创建 Relay 引擎（角色 outbox_relay_role，连接池参数与应用侧一致）。

    进程入口传自己的 ``get_process_settings("relay")``：连接池参数、DSN 角色
    与进程身份来自同一份配置上下文（审查 P1-04）。此前 relay 入口自己
    ``create_async_engine(...)`` 并复制一遍 pool 参数，等于把连接池口径复制
    到第二个地方，改一处漏一处。
    """
    return _make_engine(build_relay_database_url(settings))


def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """基于给定引擎构建会话工厂（共享同一批事务/刷新约定）。

    约定：``expire_on_commit=False``（提交后属性仍可用，避免惰性刷新）、
    ``autoflush=False``（刷新时机显式控制，避免隐式 flush 与并发条件更新
    相互干扰）。生产与测试共用本函数，保证会话语义一致。
    """
    return async_sessionmaker(
        engine,
        expire_on_commit=False,
        autoflush=False,
    )


async def probe_domain_databases(app: FastAPI) -> ReadinessResult:
    """真实探测两个域连接池 + 迁移版本，返回就绪判定。

    为什么不探第三个池：``alembic_version`` 全库共享，从 project 池读一次即可。

    为什么逐个记录 check 名：unready 时运维需要知道「是 asset 池挂了还是
    project 池挂了」，一个笼统的 ``database: down`` 会把排障从「看一眼」变成
    「逐个连一遍」。失败时不记录已通过的项也无妨——``checks`` 只描述结论。

    连接池按域拆分后必须**两个池都探活**：只探 project 池时，asset 凭据或角色
    权限损坏仍会回报 ready，资产路由的流量进来变成 500。

    ``schema_revision`` 一并回报：本机曾有两个 Postgres 共用 15432 端口的不同
    地址族（127.0.0.1 → 旧集群、localhost → Docker），「连错库」表现为「迁移像是
    没跑」。读不到版本表同样按 unready 处理（fail-closed，不猜）。

    归因在**探测内部**完成，不外抛给探针兜底：外层只能给出笼统的
    ``probe_failed``，而运维要的是「哪个池挂了」。异常在这里就地转成对应的
    check 名，探针层的兜底只负责「检查函数自身崩溃/超时」这类真正意外。
    """
    checks: dict[str, str] = {}
    for name in (
        "project_planning_sessionmaker",
        "asset_service_sessionmaker",
        "production_planning_sessionmaker",
        "budget_quota_sessionmaker",
        "qc_governance_sessionmaker",
    ):
        label = name.removesuffix("_sessionmaker") + "_database"
        try:
            async with getattr(app.state, name)() as session:
                await session.execute(text("SELECT 1"))
        except Exception:
            logger.error("readiness_check_failed", check=label, exc_info=True)
            return ReadinessResult(ready=False, checks={label: "down"})
        checks[label] = "up"

    try:
        async with app.state.project_planning_sessionmaker() as session:
            revision = (
                await session.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar_one_or_none()
    except Exception:
        # 版本表读的是 project 池：归因到它就够定位，不必再编一个 check 名。
        logger.error(
            "readiness_check_failed", check="schema_revision", exc_info=True
        )
        return ReadinessResult(
            ready=False, checks={"project_planning_database": "down"}
        )
    return ReadinessResult(ready=True, checks=checks, schema_revision=revision)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """FastAPI Lifespan：管理各业务域引擎与会话工厂的生命周期（P0-4 根治核心）。

    DATA-1（模块级角色隔离）：project_planning 与 asset_service 各自独立
    引擎 + 会话工厂，以各自数据库角色连接；业务代码按域注入对应工作单元，
    使「跨域越界写」在数据库层被拒绝（而非依赖代码自觉）。

    - 启动：创建两个域的引擎与会话工厂，挂到 ``app.state`` 供依赖层读取。
      同时保留 ``app.state.sessionmaker``（app_role）指向 project_planning
      域会话工厂，兼容 /readyz 与既有测试装配。
    - 关闭：逐个 ``engine.dispose()`` 释放连接池，避免进程退出时 asyncpg
      连接泄漏。

    引擎只在其被创建的运行循环内使用；循环的创建/销毁由 uvicorn 的
    生命周期统一负责，业务代码不再持有任何全局引擎引用或循环亲和性判断。
    """
    project_engine = create_project_planning_engine()
    asset_engine = create_asset_service_engine()
    production_engine = create_production_planning_engine()
    budget_engine = create_budget_quota_engine()
    qc_engine = create_qc_governance_engine()
    project_sessionmaker = create_sessionmaker(project_engine)
    asset_sessionmaker = create_sessionmaker(asset_engine)
    app.state.project_planning_engine = project_engine
    app.state.asset_service_engine = asset_engine
    app.state.production_planning_engine = production_engine
    app.state.budget_quota_engine = budget_engine
    app.state.qc_governance_engine = qc_engine
    app.state.project_planning_sessionmaker = project_sessionmaker
    app.state.asset_service_sessionmaker = asset_sessionmaker
    app.state.production_planning_sessionmaker = create_sessionmaker(production_engine)
    app.state.budget_quota_sessionmaker = create_sessionmaker(budget_engine)
    app.state.qc_governance_sessionmaker = create_sessionmaker(qc_engine)
    # 向后兼容别名：既有代码读 app.state.sessionmaker 时，落到
    # project_planning 域会话工厂（有 public.alembic_version 只读权限）。
    # /readyz 已不依赖此别名——池拆分后它显式探测两个域池（Codex P2）。
    app.state.engine = project_engine
    app.state.sessionmaker = project_sessionmaker
    # RLS 生效性验证（ADR-R24 §D3a-5）：业务连接是不是超级用户/BYPASSRLS/表所有者。
    # 放在启动期而不是每请求：运行期发现「安全属性未知」时请求已经在跑了；放在
    # 建池之后、接流量之前，是因为它必须用**即将用于业务查询的同一条连接**去问。
    # 生产环境失败即拒绝启动；开发环境降级为告警（本地用 postgres 连库是既有事实）。
    settings = get_settings()
    # 逐池显式调用（而不是一个循环）：启动期「哪个池验过 RLS」必须能在代码里
    # 一眼数出来，边界断言也是按调用点数（一个不接线的检查等于没有检查）。
    await verify_rls_enforcement(project_engine, app_env=settings.app_env)
    await verify_rls_enforcement(asset_engine, app_env=settings.app_env)
    await verify_rls_enforcement(production_engine, app_env=settings.app_env)
    await verify_rls_enforcement(budget_engine, app_env=settings.app_env)
    await verify_rls_enforcement(qc_engine, app_env=settings.app_env)
    # 限流器与数据库同属「进程级基础设施」，生命周期统一在这里收口：
    # 漏关连接池比漏开更隐蔽，集中一处才不会各写一遍。
    app.state.rate_limiter = RateLimiter.from_settings()
    # 就绪探针：探测逻辑在这里（它探的就是本模块管的池），缓存/单飞/超时在
    # ReadinessProbe 里。/readyz 因此是内存读，不会每个请求真打三次连接。
    app.state.readiness_probe = ReadinessProbe(
        lambda: probe_domain_databases(app),
        ttl_seconds=settings.readiness_cache_ttl_seconds,
        timeout_seconds=settings.readiness_timeout_seconds,
    )
    try:
        yield
    finally:
        # 先关闭 tracer（flush 缓冲 span），再释放限流与数据库连接池。
        await app.state.rate_limiter.aclose()
        shutdown_telemetry()
        for domain_engine in (
            project_engine,
            asset_engine,
            production_engine,
            budget_engine,
            qc_engine,
        ):
            await domain_engine.dispose()
