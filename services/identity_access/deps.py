"""身份治理依赖统一装配（供各 API 路由复用）。

职责：把「认证（Principal）+ RLS 会话（tenant 上下文）」组装为可直接注入
FastAPI 路由的依赖对象，避免每个路由重复拼装 KeyStore / 依赖工厂。

依赖链（单向，无循环）：
    get_principal     —— Authorization Bearer → Principal（验签）
    get_tenant_session —— Principal → SET LOCAL app.current_tenant_id → session
    get_service       —— get_tenant_session → 业务 Service（各路由自行装配仓储）

路由只做两件事：注入 principal / tenant_session，把身份与租户上下文交给
服务层，绝不从请求体读身份字符串。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from packages.identity.principal import Principal
from packages.shared.config import get_settings
from packages.shared.rls import build_tenant_session_dependency
from services.identity_access import get_public_key_store
from services.identity_access.auth_dependency import build_principal_dependency
from services.identity_access.rbac import build_authorize_dependency

# 认证依赖：Bearer → Principal（验签 + issuer/audience 校验）。
get_principal = build_principal_dependency(
    key_store=get_public_key_store(),
    issuer=get_settings().identity_issuer,
    audience=get_settings().identity_audience,
)

# RLS 会话依赖：Principal → 注入 tenant 上下文 → session。
get_tenant_session = build_tenant_session_dependency(get_principal)


def require_role(action_key: str) -> Callable[..., Awaitable[Principal]]:
    """构造绑定 ``get_principal`` 的角色授权依赖（所有写入口的统一授权闸门）。

    每个写路由按自身动作注入，例如 ``Depends(require_role("PROJECT.CREATE"))``。
    依赖先复用 ``get_principal`` 解析可信 Principal，再用其 roles 与
    ``action_registry[action_key].allowed_principals`` 求交，越权抛 403。
    """
    return build_authorize_dependency(get_principal, action_key)


__all__ = [
    "Principal",
    "get_principal",
    "get_tenant_session",
    "require_role",
]
