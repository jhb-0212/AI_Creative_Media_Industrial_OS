"""运行时 RBAC 授权依赖（把 action_registry.allowed_principals 接到依赖层）。

设计要点（P0-3 收口 R6/R9：安全必须真实执行）：
- 此前 Principal 只用于身份解析与 RLS 租户上下文，roles 从未参与运行时判定，
  权限矩阵仍是 YAML 声明。本模块把 ``allowed_principals`` 解析为角色集合，
  在 FastAPI 依赖层用 ``Principal.roles`` 做真实校验，越权抛 403。
- fail-closed：未注册 action_key、空授权、角色不匹配，一律拒绝，绝不放行。
- 依赖工厂与认证依赖解耦（传入 ``get_principal``），保持单向依赖、无循环。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import Depends

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.identity.principal import Principal
from services.identity_access.action_registry import allowed_roles

# 认证依赖函数类型别名：解析出 Principal 的异步可调用对象。
PrincipalDependency = Callable[..., Awaitable[Principal]]


def build_authorize_dependency(
    get_principal: PrincipalDependency,
    action_key: str,
) -> Callable[..., Awaitable[Principal]]:
    """构造指定 action 的角色授权依赖（供 ``Depends(...)`` 注入）。

    依赖链：``authorize -> get_principal``（复用认证依赖，FastAPI 缓存去重）。
    判定：``Principal.roles`` 与 ``action_registry[action_key].allowed_principals``
    求交集，交集非空则放行并原样返回 Principal（供路由继续取身份/租户），
    否则抛 403 ``AUTH_SCOPE_DENIED``。
    """

    async def authorize(
        principal: Principal = Depends(get_principal),
    ) -> Principal:
        allowed = allowed_roles(action_key)
        if not allowed:
            raise DomainError(
                ErrorRegistry.AUTH_SCOPE_DENIED,
                detail=f"动作 {action_key} 未配置允许角色",
            )
        if not (principal.roles & allowed):
            raise DomainError(
                ErrorRegistry.AUTH_SCOPE_DENIED,
                detail=f"缺少执行 {action_key} 所需角色",
            )
        return principal

    return authorize
