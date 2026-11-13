"""数据库基础设施（SQLAlchemy 异步引擎 + 会话工厂 + 声明基类）。

设计要点（数据所有权先行，14.2）：
- 每个领域模块使用独立 PostgreSQL Schema（命名空间隔离），
  本模块提供「按模块名创建 engine」的通用能力。
- 密钥不落日志：DSN 中的密码来自 SecretStr，仅用于连接，不打印。
- 连接池与超时参数显式配置，为高并发预留（pool_size / max_overflow）。
"""

from __future__ import annotations

import asyncio
import threading
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
    """构造应用连接的异步 DSN（asyncpg 驱动，应用角色 app_role）。

    RLS 生效前提：应用以非超级用户角色（app_role）连接，否则超级用户
    BYPASSRLS 导致行级安全失效（决策 5 的终极防呆不设防）。app_role 由
    迁移 9e3b4c5d6e7f 创建，仅授权 project_planning/outbox 的数据权限，
    无 DDL 权限（最小权限）。

    用户名与密码均做 URL 编码，避免特殊字符破坏连接串解析。
    """
    settings = get_settings()
    user = quote_plus(settings.postgres_app_user)
    password = quote_plus(settings.postgres_app_password.get_secret_value())
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

# 引擎创建时绑定的事件循环引用。asyncpg 的连接池与其创建时所在的
# 事件循环强绑定；一旦该循环被关闭（例如测试会话结束、进程复用），
# 旧连接池会永久挂起。这里记录绑定循环，供 get_session 做亲和性检测。
_engine_loop: asyncio.AbstractEventLoop | None = None

# 单例初始化锁：保护「engine + sessionmaker 一并构建」这一个原子操作。
# 注意：必须用 RLock（可重入锁）或保证锁内不再嵌套获取本锁，否则
# get_sessionmaker 持锁调用 get_engine 会因 threading.Lock 不可重入而死锁。
# 这里采用「单一原子初始化函数」，锁内直接构建，彻底避免嵌套锁。
_init_lock = threading.Lock()


def _build_engine_and_sessionmaker(loop: asyncio.AbstractEventLoop | None) -> None:
    """原子构建 engine + sessionmaker（须在 _init_lock 保护下调用）。

    将两者合并为一个初始化单元，避免「sessionmaker 构建时调用 get_engine
    再拿同一把锁」的嵌套锁死锁。loop 为当前运行循环（可能为 None，表示
    同步上下文）。
    """
    global _engine, _sessionmaker, _engine_loop
    _engine = create_engine()
    _engine_loop = loop
    _sessionmaker = async_sessionmaker(
        _engine,
        expire_on_commit=False,
        autoflush=False,
    )


def _current_loop_or_none() -> asyncio.AbstractEventLoop | None:
    """返回当前运行循环，若无（同步上下文）返回 None。"""
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


def get_engine() -> AsyncEngine:
    """获取全局引擎单例（线程安全，双重检查锁定）。

    注意：返回的引擎绑定到其首次创建时的事件循环。若调用方在另一个
    事件循环中复用（典型如测试会话切换），必须通过 get_session 的
    亲和性检测触发重建，否则 asyncpg 会因跨循环操作而挂起。
    """
    global _engine
    if _engine is None:
        with _init_lock:
            if _engine is None:
                _build_engine_and_sessionmaker(_current_loop_or_none())
    assert _engine is not None  # 双重检查锁定后必然已初始化
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """获取会话工厂单例（线程安全，双重检查锁定）。

    与 get_engine 共用同一个原子初始化：无论先调哪个，都会一次性构建
    engine + sessionmaker，避免分别初始化时的嵌套锁死锁。
    """
    global _sessionmaker
    if _sessionmaker is None:
        with _init_lock:
            if _sessionmaker is None:
                _build_engine_and_sessionmaker(_current_loop_or_none())
    assert _sessionmaker is not None  # 双重检查锁定后必然已初始化
    return _sessionmaker


async def _reset_engine_for_loop(current_loop: asyncio.AbstractEventLoop) -> None:
    """当引擎绑定的事件循环已失效时，安全重建引擎与会话工厂。

    这是 async 引擎「事件循环亲和性」的标准处置：asyncpg 连接池不可跨
    循环复用，必须在旧循环销毁后 dispose 旧池、在新循环上重建。
    """
    global _engine, _sessionmaker, _engine_loop
    if _engine is not None:
        await _engine.dispose()
    _engine = create_engine()
    _engine_loop = current_loop
    _sessionmaker = async_sessionmaker(
        _engine,
        expire_on_commit=False,
        autoflush=False,
    )


async def get_session() -> AsyncIterator[AsyncSession]:
    """依赖注入：提供独立事务会话（事务边界上移到请求层）。

    采用「工作单元」语义：整个请求期间共享同一会话，仓储只做持久化
    变更（merge/flush）而不再自行 commit；由本依赖在请求成功时统一
    commit、异常时统一 rollback，保证多步用例的原子性。

    事件循环亲和性：若引擎绑定循环已失效（测试会话切换、进程复用），
    先安全重建引擎，再创建会话，根治「跨循环复用连接池导致挂起」。
    """
    current_loop = asyncio.get_running_loop()

    global _engine_loop
    # 亲和性检测：
    # 1) 引擎在同步上下文创建、尚未绑定任何循环 → 补记当前循环，
    #    确保后续切换循环时能正确触发重建。
    # 2) 引擎绑定的循环与当前循环不一致（旧循环已失效）→ 重建。
    if _engine_loop is None:
        _engine_loop = current_loop
    elif _engine_loop is not current_loop:
        await _reset_engine_for_loop(current_loop)

    async with get_sessionmaker()() as session:
        try:
            yield session
            await session.commit()
        except BaseException:
            await session.rollback()
            raise
