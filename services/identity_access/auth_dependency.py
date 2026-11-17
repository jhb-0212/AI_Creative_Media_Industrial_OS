"""FastAPI 认证依赖：从 Authorization Bearer 解析可信 Principal（决策 4）。

设计要点（P0-3 根治核心）：
- 身份的唯一可信出口是 Principal，由本依赖从 JWT 验签后构造；
  业务路由只经 ``Depends(get_principal)`` 取身份，绝不读请求体字符串。
- 验签只用公钥（JWKS/KeyStore），私钥绝不进入本路径（决策 4）。
- 校验失败统一转 401 AUTH_UNAUTHENTICATED，不泄漏验签细节。

分层：本模块属于 services/identity_access（领域组装层），依赖
packages/identity（平台基础设施）与 packages/errors（错误码），
不反向依赖任何业务模块。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import replace

import jwt
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from packages.errors.error_codes import DomainError, ErrorCode, ErrorRegistry
from packages.identity.capability import (
    CAPABILITY_AUDIENCE,
    Capability,
    CapabilityError,
    CapabilityExpiredError,
    decode_capability,
)
from packages.identity.jwks import PublicKeyStore
from packages.identity.jwt_core import TokenError, decode_token
from packages.identity.principal import Principal

# 依赖函数类型别名：解析出 Principal 的异步可调用对象。
PrincipalDependency = Callable[..., Awaitable[Principal]]

# Bearer 认证头解析器（401 由 FastAPI 安全层统一处理）。
_bearer = HTTPBearer(auto_error=False)

# Capability Token 走独立请求头：与 Bearer 访问令牌分开，
# 避免把两类受众不同的 JWT 塞进同一个字段造成误用。
CAPABILITY_HEADER = "X-Capability-Token"


def extract_bearer_token(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> str:
    """从 Authorization 头提取 Bearer token，缺失则抛 401。"""
    if credentials is None or not credentials.credentials:
        raise DomainError(
            ErrorRegistry.AUTH_UNAUTHENTICATED,
            detail="缺少有效的 Authorization Bearer 凭证",
        )
    return credentials.credentials


def _jwt_kid(token: str, *, error_code: ErrorCode) -> str:
    """读取 JWT header 的 kid；两类令牌共用，但失败错误码由调用方决定。"""
    try:
        kid = jwt.get_unverified_header(token).get("kid")
    except Exception as exc:
        raise DomainError(error_code, detail="令牌格式非法") from exc
    if not isinstance(kid, str) or not kid.strip():
        raise DomainError(error_code, detail="令牌缺少 kid")
    return kid


def build_principal_dependency(
    *,
    key_store: PublicKeyStore,
    issuer: str,
    audience: str,
    revocation_check: Callable[[Request, str], Awaitable[bool]] | None = None,
    capability_issuer: str | None = None,
    capability_audience: str = CAPABILITY_AUDIENCE,
    require_capability: bool = True,
) -> PrincipalDependency:
    """构造 ``get_principal``：Bearer 身份令牌 + X-Capability-Token 能力令牌。

    两类令牌都是 RS256，都按 kid 从**纯公钥**仓储取公钥；受众不同，访问令牌
    无法被当作 capability 重放。capability 验签成功后挂到 Principal 上，
    后续所有 ``authorize()`` 调用仍走同一个决策入口，不存在第二套判定。

    ``revocation_check`` 同时检查访问令牌 jti 与 capability jti；任一被吊销
    即拒绝。缺 capability 默认是 403（契约里所有动作 token_required=true）。
    """

    async def _verify_capability(request: Request, raw: str | None) -> Capability | None:
        if raw is None or not raw.strip():
            if not require_capability:
                return None
            raise DomainError(
                ErrorRegistry.AUTH_SCOPE_DENIED,
                detail=f"缺少 {CAPABILITY_HEADER} 短时能力凭证",
            )

        kid = _jwt_kid(raw, error_code=ErrorRegistry.AUTH_SCOPE_DENIED)
        public_pem = key_store.public_pem(kid)
        if public_pem is None:
            raise DomainError(
                ErrorRegistry.AUTH_SCOPE_DENIED, detail="Capability 签名密钥未知"
            )
        try:
            capability = decode_capability(
                raw,
                public_pem=public_pem,
                issuer=capability_issuer or issuer,
                audience=capability_audience,
            )
        except CapabilityExpiredError as exc:
            raise DomainError(ErrorRegistry.CAPABILITY_EXPIRED) from exc
        except CapabilityError as exc:
            raise DomainError(
                ErrorRegistry.AUTH_SCOPE_DENIED, detail="Capability Token 无效"
            ) from exc

        if revocation_check is not None and await revocation_check(request, capability.jti):
            raise DomainError(ErrorRegistry.CAPABILITY_REVOKED)
        return capability

    async def get_principal(
        request: Request,
        token: str = Depends(extract_bearer_token),
    ) -> Principal:
        """解析两类令牌为可信 Principal（验签 + claim + 吊销校验）。"""
        kid = _jwt_kid(token, error_code=ErrorRegistry.AUTH_UNAUTHENTICATED)
        public_pem = key_store.public_pem(kid)
        if public_pem is None:
            raise DomainError(
                ErrorRegistry.AUTH_UNAUTHENTICATED, detail="未知的签名密钥"
            )

        try:
            principal = decode_token(
                token,
                public_pem=public_pem,
                issuer=issuer,
                audience=audience,
            )
        except TokenError as exc:
            raise DomainError(
                ErrorRegistry.AUTH_UNAUTHENTICATED, detail="令牌验签失败"
            ) from exc

        if revocation_check is not None:
            if not principal.jti:
                raise DomainError(
                    ErrorRegistry.AUTH_UNAUTHENTICATED, detail="令牌缺少 jti"
                )
            if await revocation_check(request, principal.jti):
                raise DomainError(
                    ErrorRegistry.AUTH_UNAUTHENTICATED, detail="令牌已吊销"
                )

        capability = await _verify_capability(
            request, request.headers.get(CAPABILITY_HEADER)
        )
        return replace(principal, capability=capability)

    return get_principal
