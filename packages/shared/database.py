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
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from packages.shared.config import get_settings
from packages.shared.rate_limit import RateLimiter
from packages.telemetry.tracing import shutdown_telemetry


class Base(DeclarativeBase):
    """统一声明基类，供各领域模块的 ORM 模型继承。"""

    pass


def build_database_url() -> str:
    """构造应用连接的异步 DSN（asyncpg 驱动，应用角色 app_role）。

    RLS 生效前提：应用以非超级用户角色（app_role）连接，否则超级用户
    BYPASSRLS 导致行级安全失效（决策 5 的终极防呆不设防）。app_role 由
    迁移 9e3b4c5d6e7f 创建，仅授权 project_planning/outbox 的数据权限，
    无 DDL 权限（最小权限）。

    用户名与密码均做 URL 编码，避免特殊字符破坏连接串解析。
    用 ``quote(safe="")`` 而非 ``quote_plus``：userinfo 里 ``+`` 是字面字符，
    密码含空格时若编码成 ``+`` 会被当作字面加号，导致凭据解错。
    """
    settings = get_settings()
    user = quote(settings.postgres_app_user, safe="")
    password = quote(settings.postgres_app_password.get_secret_value(), safe="")
    return (
        f"postgresql+asyncpg://{user}:{password}"
        f"@{settings.postgres_host}:{settings.postgres_port}/{settings.postgres_db}"
    )


def build_migration_url() -> str:
    """构造迁移（alembic）连接的异步 DSN（超级用户，需 DDL 权限）。

    迁移需要 CREATE/ALTER/DROP schema、表、角色等 DDL 权限，必须用超级
    用户（postgres）连接；应用运行时才用 app_role（无 DDL 权限）。二者
    职责分离，避免应用角色拥有过高权限。
    """
    settings = get_settings()
    user = quote(settings.postgres_user, safe="")
    password = quote(settings.postgres_password.get_secret_value(), safe="")
    return (
        f"postgresql+asyncpg://{user}:{password}"
        f"@{settings.postgres_host}:{settings.postgres_port}/{settings.postgres_db}"
    )


def build_relay_database_url() -> str:
    """构造 Outbox Relay 连接的异步 DSN（专用角色 outbox_relay_role）。

    Relay 只消费 outbox 事件并回写投递状态，以独立角色连接（F03 数据所有权
    的进程边界隔离）；该角色仅持有 outbox schema 的 SELECT + UPDATE 权限，
    不持有任何业务 schema 的写权限，使越界写业务表在数据库层被拒绝。
    """
    settings = get_settings()
    user = quote(settings.postgres_relay_user, safe="")
    password = quote(settings.postgres_relay_password.get_secret_value(), safe="")
    return (
        f"postgresql+asyncpg://{user}:{password}"
        f"@{settings.postgres_host}:{settings.postgres_port}/{settings.postgres_db}"
    )


def build_project_planning_database_url() -> str:
    """构造 project_planning 域连接的异步 DSN（角色 project_planning_role）。

    DATA-1（模块级角色隔离）：project_planning 域以独立角色连接，只持有
    project_planning schema 的写权限 + outbox 写 + auth 只读，不得写
    asset_service schema，使跨域越界写在数据库层被拒绝。
    """
    settings = get_settings()
    user = quote(settings.postgres_project_planning_user, safe="")
    password = quote(
        settings.postgres_project_planning_password.get_secret_value(), safe=""
    )
    return (
        f"postgresql+asyncpg://{user}:{password}"
        f"@{settings.postgres_host}:{settings.postgres_port}/{settings.postgres_db}"
    )


def build_asset_service_database_url() -> str:
    """构造 asset_service 域连接的异步 DSN（角色 asset_service_role）。

    DATA-1（模块级角色隔离）：asset_service 域以独立角色连接，只持有
    asset_service schema 的写权限 + outbox 写 + auth 只读，不得写
    project_planning schema，使跨域越界写在数据库层被拒绝。
    """
    settings = get_settings()
    user = quote(settings.postgres_asset_service_user, safe="")
    password = quote(settings.postgres_asset_service_password.get_secret_value(), safe="")
    return (
        f"postgresql+asyncpg://{user}:{password}"
        f"@{settings.postgres_host}:{settings.postgres_port}/{settings.postgres_db}"
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
    project_sessionmaker = create_sessionmaker(project_engine)
    asset_sessionmaker = create_sessionmaker(asset_engine)
    app.state.project_planning_engine = project_engine
    app.state.asset_service_engine = asset_engine
    app.state.project_planning_sessionmaker = project_sessionmaker
    app.state.asset_service_sessionmaker = asset_sessionmaker
    # 向后兼容别名：/readyz 与既有代码读 app.state.sessionmaker 时，落到
    # project_planning 域会话工厂（有 public.alembic_version 只读权限）。
    app.state.engine = project_engine
    app.state.sessionmaker = project_sessionmaker
    # 限流器与数据库同属「进程级基础设施」，生命周期统一在这里收口：
    # 漏关连接池比漏开更隐蔽，集中一处才不会各写一遍。
    app.state.rate_limiter = RateLimiter.from_settings()
    try:
        yield
    finally:
        # 先关闭 tracer（flush 缓冲 span），再释放限流与数据库连接池。
        await app.state.rate_limiter.aclose()
        shutdown_telemetry()
        await project_engine.dispose()
        await asset_engine.dispose()
