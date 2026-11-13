"""数据库基础设施（SQLAlchemy 异步引擎 + 会话工厂 + 声明基类）。

设计要点（数据所有权先行，14.2）：
- 每个领域模块使用独立 PostgreSQL Schema（命名空间隔离），
  本模块提供「按模块名创建 engine」的通用能力。
- 密钥不落日志：DSN 中的密码来自 SecretStr，仅用于连接，不打印。
- 连接池与超时参数显式配置，为高并发预留（pool_size / max_overflow）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from urllib.parse import quote_plus

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from packages.shared.config import get_settings


class Base(DeclarativeBase):
    """统一声明基类，供各领域模块的 ORM 模型继承。"""

    pass


def build_database_url() -> str:
    """构造异步 DSN（asyncpg 驱动）。

    用户名与密码均做 URL 编码，避免特殊字符（如 @ : / # 空格）破坏连接串解析。
    """
    settings = get_settings()
    user = quote_plus(settings.postgres_user)
    password = quote_plus(settings.postgres_password.get_secret_value())
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


_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    """获取全局引擎单例。"""
    global _engine
    if _engine is None:
        _engine = create_engine()
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """获取会话工厂单例。"""
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(
            get_engine(),
            expire_on_commit=False,
            autoflush=False,
        )
    return _sessionmaker


async def get_session() -> AsyncIterator[AsyncSession]:
    """依赖注入：提供独立事务会话（事务边界上移到请求层）。

    采用「工作单元」语义：整个请求期间共享同一会话，仓储只做持久化
    变更（merge/flush）而不再自行 commit；由本依赖在请求成功时统一
    commit、异常时统一 rollback，保证多步用例的原子性。
    """
    async with get_sessionmaker()() as session:
        try:
            yield session
            await session.commit()
        except BaseException:
            await session.rollback()
            raise
