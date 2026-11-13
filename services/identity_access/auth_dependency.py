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

import jwt
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.identity.jwks import KeyStore, PublicKeyStore
from packages.identity.jwt_core import TokenError, decode_token
from packages.identity.principal import Principal

# 依赖函数类型别名：解析出 Principal 的异步可调用对象。
PrincipalDependency = Callable[..., Awaitable[Principal]]

# Bearer 认证头解析器（401 由 FastAPI 安全层统一处理）。
_bearer = HTTPBearer(auto_error=False)


def _extract_bearer_token(
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


def build_principal_dependency(
    *,
    key_store: KeyStore | PublicKeyStore,
    issuer: str,
    audience: str,
) -> PrincipalDependency:
    """构造一个可注入的 ``get_principal`` FastAPI 依赖工厂。

    参数：
    - key_store：持有一组签名密钥（验签用公钥按 kid 索引）。
    - issuer / audience：令牌签发方与目标受众（严格校验，拒绝跨服务令牌）。

    返回：一个 ``Depends(get_principal)`` 可用的依赖函数，解析出 Principal。
    """

    async def get_principal(
        token: str = Depends(_extract_bearer_token),
    ) -> Principal:
        """解析 Authorization Bearer 为可信 Principal（验签 + claim 校验）。

        验签步骤：
        1. 解码 token header 取 kid（jwt.get_unverified_header）。
        2. 按 kid 从 KeyStore 取公钥。
        3. 公钥验签 + issuer/audience/过期校验，构造 Principal。
        任一失败抛 401 AUTH_UNAUTHENTICATED（不泄漏内部细节）。
        """
        try:
            kid = jwt.get_unverified_header(token).get("kid")
        except Exception as exc:
            raise DomainError(
                ErrorRegistry.AUTH_UNAUTHENTICATED, detail="令牌格式非法"
            ) from exc

        if not kid:
            raise DomainError(
                ErrorRegistry.AUTH_UNAUTHENTICATED, detail="令牌缺少 kid"
            )

        public_pem = key_store.public_pem(kid)
        if public_pem is None:
            raise DomainError(
                ErrorRegistry.AUTH_UNAUTHENTICATED, detail="未知的签名密钥"
            )

        try:
            return decode_token(
                token,
                public_pem=public_pem,
                issuer=issuer,
                audience=audience,
            )
        except TokenError as exc:
            raise DomainError(
                ErrorRegistry.AUTH_UNAUTHENTICATED, detail="令牌验签失败"
            ) from exc

    return get_principal
