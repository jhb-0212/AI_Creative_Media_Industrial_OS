"""令牌吊销检查（jti denylist，P3-2）。

背景：JWT 签发时已经生成唯一 `jti`，但全仓没有任何吊销路径——令牌泄露后
只能等 15 分钟自然过期。对"导演/制片"这类高权限角色，15 分钟的窗口不可接受。

设计取舍：
- **denylist 而非 allowlist**：验签成功是常态，吊销是例外；每次请求一次
  `auth.revoked_tokens` 主键查询（jti 唯一索引），成本是一次轻量 SELECT。
- **fail-closed**：拿不到会话工厂或查询失败时不放行（抛 401），宁可拒绝
  一个合法请求，也不放行一个已吊销令牌。
- 无 logout 契约动作：吊销只经运维 CLI（scripts/revoke_token.py）执行，
  不新增 HTTP 端点（不猜契约）。
"""

from __future__ import annotations

from typing import Any

from fastapi import Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.errors.error_codes import DomainError, ErrorRegistry

_IS_REVOKED = text(
    """
    SELECT 1
    FROM auth.revoked_tokens
    WHERE jti = :jti AND expires_at > now()
    LIMIT 1
    """
)


async def is_token_revoked(sessionmaker: async_sessionmaker[AsyncSession], jti: str) -> bool:
    """查询 jti 是否在吊销表内（仅统计仍有效的吊销记录）。

    fail-closed（模块 docstring）：查询本身失败（数据库不可达 / 连接超时 /
    表缺失等）时不返回 False，而是抛 DomainError(AUTH_UNAUTHENTICATED)——
    无法确认吊销状态一律按「拒绝」处理，且口径与认证失败一致（统一 401 信封），
    避免把数据库抖动误报成服务端 500。
    """
    try:
        async with sessionmaker() as session:
            row = await session.execute(_IS_REVOKED, {"jti": jti})
            return row.first() is not None
    except DomainError:
        raise
    except Exception as exc:
        raise DomainError(
            ErrorRegistry.AUTH_UNAUTHENTICATED,
            detail="无法校验令牌吊销状态",
        ) from exc


def build_revocation_check(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> Any:
    """构造供认证依赖使用的吊销检查回调（绑定会话工厂）。"""

    async def check(_request: Request, jti: str) -> bool:
        return await is_token_revoked(sessionmaker, jti)

    return check


async def check_request_revocation(request: Request, jti: str) -> bool:
    """从 app.state 取会话工厂并检查吊销（FastAPI 依赖链专用）。"""
    sessionmaker: async_sessionmaker[AsyncSession] | None = getattr(
        request.app.state, "sessionmaker", None
    )
    if sessionmaker is None:
        # fail-closed：无法确认吊销状态时拒绝（详见模块 docstring）。
        raise DomainError(
            ErrorRegistry.AUTH_UNAUTHENTICATED,
            detail="无法校验令牌吊销状态",
        )
    return await is_token_revoked(sessionmaker, jti)
