"""令牌吊销检查的 fail-closed 行为（复核 R4）。

要证明的：
1. 查询本身成功、jti 在吊销表 → True（正常放行拒绝路径）。
2. 查询本身失败（数据库不可达 / 连接超时）→ 抛 DomainError(AUTH_UNAUTHENTICATED)，
   而不是让 OperationalError 冒泡成 500（违反「认证失败统一 401」口径）。

本文件是纯单元测试：用桩 sessionmaker 抛异常，不依赖真实 PostgreSQL。
"""

from __future__ import annotations

import pytest

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.identity.token_revocation import is_token_revoked


class _ExplodingSessionMaker:
    """桩会话工厂：一进入上下文就抛数据库异常。"""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def __call__(self) -> _ExplodingContext:
        return _ExplodingContext(self._exc)


class _ExplodingContext:
    """桩会话上下文：__aenter__ 抛数据库异常。"""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def __aenter__(self) -> None:
        raise self._exc

    async def __aexit__(self, *args: object) -> None:
        return None


async def test_query_failure_fails_closed() -> None:
    """吊销查询抛数据库异常 → 转 401 AUTH_UNAUTHENTICATED（fail-closed）。"""
    # OperationalError 是 sqlalchemy 常见的数据库连接失败异常。
    from sqlalchemy.exc import OperationalError

    sessionmaker = _ExplodingSessionMaker(
        OperationalError("SELECT 1", {}, Exception("connection refused"))
    )
    with pytest.raises(DomainError) as exc_info:
        await is_token_revoked(sessionmaker, jti="some-jti")  # type: ignore[arg-type]
    assert exc_info.value.error_code == ErrorRegistry.AUTH_UNAUTHENTICATED
    assert "吊销" in exc_info.value.detail


async def test_query_failure_wraps_arbitrary_exception() -> None:
    """任意数据库异常（非 OperationalError）同样 fail-closed，不裸冒泡。"""
    sessionmaker = _ExplodingSessionMaker(RuntimeError("backend down"))
    with pytest.raises(DomainError) as exc_info:
        await is_token_revoked(sessionmaker, jti="some-jti")  # type: ignore[arg-type]
    assert exc_info.value.error_code == ErrorRegistry.AUTH_UNAUTHENTICATED


async def test_existing_domain_error_passes_through() -> None:
    """已有 DomainError 不应被二次包装（保留原始错误语义）。"""
    original = DomainError(ErrorRegistry.INTERNAL_ERROR, detail="原始错误")
    sessionmaker = _ExplodingSessionMaker(original)
    with pytest.raises(DomainError) as exc_info:
        await is_token_revoked(sessionmaker, jti="some-jti")  # type: ignore[arg-type]
    assert exc_info.value is original
