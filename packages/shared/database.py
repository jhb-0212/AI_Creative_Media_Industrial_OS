"""数据库基础设施（按域隔离的异步引擎 + 会话工厂 + 声明基类）。

设计要点（P0-4 根治 + 审查第三轮 F2 数据所有权）：
- **每个业务域一个连接池、一个数据库角色**（见 ``packages.shared.db_roles``）。
  连接身份在建立连接时就固定，不靠"运行时 SET ROLE"或"代码约定"来收窄权限：
  用错池 = 数据库拒绝，而不是"记得切角色"。
- 引擎与会话工厂由 FastAPI Lifespan 统一管理，挂到 ``app.state.pools``，
  随应用启动/关闭创建/释放 asyncpg 连接池；不再使用全局单例 + 事件循环动态重建。
- 事务边界由 ``packages.shared.unit_of_work`` 统一提供（API 与 Worker 同一实现），
  本模块只负责引擎/会话工厂与声明基类，不参与提交语义。
- 测试由 conftest 注入独立 ``DomainPools``，生产代码零测试感知。
- 密钥不落日志：DSN 中的密码来自 SecretStr，仅用于连接，不打印。
- 连接池与超时参数显式配置，为高并发预留（pool_size / max_overflow）；
  分域后单池更小，避免一个慢域把整池连接占满拖垮其它域。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from types import MappingProxyType
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
from packages.shared.db_roles import DatabaseDomain, role_for
from packages.shared.rate_limit import RateLimiter
from packages.telemetry.tracing import shutdown_telemetry

# 单个域连接池的上限。分域后不再需要"一个池扛全部流量"，
# 因此比单一池的 10/20 收紧：四域合计仍有余量，但任一域故障不会吃光全部。
_POOL_SIZE = 5
_MAX_OVERFLOW = 10


class Base(DeclarativeBase):
    """统一声明基类，供各领域模块的 ORM 模型继承。"""

    pass


def build_database_url(domain: DatabaseDomain) -> str:
    """构造某业务域的异步 DSN（asyncpg 驱动，该域的专用最小权限角色）。

    RLS 生效前提：连接角色必须是 ``NOSUPERUSER`` / ``NOBYPASSRLS``；域角色由
    迁移 ``c2d3e4f5a6b7`` 创建，只被授予本域 schema 的表权限，无 DDL 权限。

    用户名与密码均做 URL 编码，避免特殊字符破坏连接串解析。
    用 ``quote(safe="")`` 而非 ``quote_plus``：userinfo 里 ``+`` 是字面字符，
    密码含空格时若编码成 ``+`` 会被当作字面加号，导致凭据解错。

    调用方必须显式给域，没有"默认域"：默认值会让忘记指定的代码悄悄以某个
    角色的身份运行，正是本轮要消灭的那类越权路径。
    """
    settings = get_settings()
    user = quote(role_for(domain), safe="")
    password = quote(settings.postgres_app_password.get_secret_value(), safe="")
    return (
        f"postgresql+asyncpg://{user}:{password}"
        f"@{settings.postgres_host}:{settings.postgres_port}/{settings.postgres_db}"
    )


def build_migration_url() -> str:
    """构造迁移（alembic）连接的异步 DSN（超级用户，需 DDL 权限）。

    迁移需要 CREATE/ALTER/DROP schema、表、角色等 DDL 权限，必须用超级
    用户（postgres）连接；应用运行时只用按域拆分的业务角色（无 DDL 权限）。
    二者职责分离，避免应用侧拥有过高权限。
    """
    settings = get_settings()
    user = quote(settings.postgres_user, safe="")
    password = quote(settings.postgres_password.get_secret_value(), safe="")
    return (
        f"postgresql+asyncpg://{user}:{password}"
        f"@{settings.postgres_host}:{settings.postgres_port}/{settings.postgres_db}"
    )


def create_engine(domain: DatabaseDomain) -> AsyncEngine:
    """为某业务域创建异步引擎（该域专用连接池与角色）。"""
    return create_async_engine(
        build_database_url(domain),
        echo=False,
        pool_size=_POOL_SIZE,
        max_overflow=_MAX_OVERFLOW,
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


@dataclass(frozen=True)
class _DomainPool:
    """单个域的引擎 + 会话工厂（两者必须成对存在，避免只关其中一个）。"""

    engine: AsyncEngine
    sessionmaker: async_sessionmaker[AsyncSession]

    @classmethod
    def create(cls, domain: DatabaseDomain) -> _DomainPool:
        engine = create_engine(domain)
        return cls(engine=engine, sessionmaker=create_sessionmaker(engine))


class DomainPools:
    """全部业务域的引擎与会话工厂集合（进程级唯一实例）。

    为什么把四个域放在一个对象里，而不是四个全局变量：
    - 生命周期必须一起收口。漏关一个池比漏开更隐蔽，而"每个域各写一遍
      startup/shutdown"最终一定有一个域被漏掉。
    - ``sessionmaker(domain)`` 是唯一取池入口，调用方必须说明"我是哪个域"；
      没有"默认池"，避免忘记指定时悄悄借用别的域身份。

    不可变：构造后池集合不再变化，避免运行期换池导致连接身份不一致。
    """

    def __init__(self, pools: Mapping[DatabaseDomain, _DomainPool]) -> None:
        self._pools = MappingProxyType(dict(pools))

    @classmethod
    def create(cls) -> DomainPools:
        """按 ``DatabaseDomain`` 全集建池（缺域即启动失败，不做部分可用）。"""
        return cls({domain: _DomainPool.create(domain) for domain in DatabaseDomain})

    def sessionmaker(
        self, domain: DatabaseDomain
    ) -> async_sessionmaker[AsyncSession]:
        """取某域的会话工厂（未建池的域 ``KeyError``，不回退）。"""
        return self._pools[domain].sessionmaker

    async def dispose(self) -> None:
        """释放全部域连接池。

        逐个释放：一个域关闭失败不应让其余池泄漏。首个异常在全部尝试后抛出
        （关闭路径同样需要可见的失败，而不是静默吞掉）。
        """
        first_error: Exception | None = None
        for domain in DatabaseDomain:
            try:
                await self._pools[domain].engine.dispose()
            except Exception as exc:  # pragma: no cover - 仅关闭路径
                first_error = first_error or exc
        if first_error is not None:
            raise first_error


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """FastAPI Lifespan：管理按域隔离的连接池生命周期（P0-4 根治核心）。

    - 启动：创建全部域连接池，挂到 ``app.state.pools``，供依赖层按域读取。
    - 关闭：逐个 ``engine.dispose()``，避免进程退出时 asyncpg 连接泄漏。

    引擎只在其被创建的运行循环内使用；循环的创建/销毁由 uvicorn 的
    生命周期统一负责，业务代码不再持有任何全局引擎引用或循环亲和性判断。
    """
    pools = DomainPools.create()
    app.state.pools = pools
    # 限流器与数据库同属「进程级基础设施」，生命周期统一在这里收口：
    # 漏关连接池比漏开更隐蔽，集中一处才不会各写一遍。
    app.state.rate_limiter = RateLimiter.from_settings()
    try:
        yield
    finally:
        # 先关闭 tracer（flush 缓冲 span），再释放限流与数据库连接池。
        await app.state.rate_limiter.aclose()
        shutdown_telemetry()
        await pools.dispose()
