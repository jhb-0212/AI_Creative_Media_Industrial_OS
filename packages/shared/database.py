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


def create_engine() -> AsyncEngine:
    """创建异步引擎（连接池参数为高并发预留）。"""
    return create_async_engine(
        build_database_url(),
        echo=False,
        pool_size=10,
        max_overflow=20,
        pool_pre_ping=True,
        pool_recycle=1800,
    )


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
    """FastAPI Lifespan：管理引擎与会话工厂的生命周期（P0-4 根治核心）。

    - 启动：创建引擎与会话工厂，挂到 ``app.state``，供依赖层读取。
    - 关闭：``engine.dispose()`` 释放连接池，避免进程退出时 asyncpg
      连接泄漏。

    引擎只在其被创建的运行循环内使用；循环的创建/销毁由 uvicorn 的
    生命周期统一负责，业务代码不再持有任何全局引擎引用或循环亲和性判断。
    """
    engine = create_engine()
    sessionmaker = create_sessionmaker(engine)
    app.state.engine = engine
    app.state.sessionmaker = sessionmaker
    # 限流器与数据库同属「进程级基础设施」，生命周期统一在这里收口：
    # 漏关连接池比漏开更隐蔽，集中一处才不会各写一遍。
    app.state.rate_limiter = RateLimiter.from_settings()
    try:
        yield
    finally:
        # 先关闭 tracer（flush 缓冲 span），再释放限流与数据库连接池。
        await app.state.rate_limiter.aclose()
        shutdown_telemetry()
        await engine.dispose()
