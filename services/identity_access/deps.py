"""身份治理依赖统一装配（供各 API 路由复用）。

职责：把「认证（Principal）+ RLS 租户上下文 + 工作单元」组装为可直接注入
FastAPI 路由的依赖对象，避免每个路由重复拼装 KeyStore / 依赖工厂。

依赖链（单向，无循环）：
    get_principal        —— Authorization Bearer → Principal（验签）
    get_tenant_uow       —— Principal → SET LOCAL app.current_tenant_id → 写工作单元
    get_read_tenant_uow  —— 同上，只读工作单元（数据库层拒绝写语句）

事务约定（R07）：写用例在返回响应前显式 ``await uow.commit()``；
不依赖 yield 依赖退出码提交（退出码在响应发出之后执行）。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from packages.identity.principal import Principal
from packages.identity.token_revocation import check_request_revocation
from packages.shared.config import get_settings
from packages.shared.rls import build_tenant_uow_dependency
from packages.shared.unit_of_work import (
    get_read_uow,
    get_uow,
    make_read_uow_dependency,
    make_uow_dependency,
)
from services.identity_access import get_public_key_store
from services.identity_access.auth_dependency import build_principal_dependency
from services.identity_access.rbac import build_authorize_dependency

# 认证依赖：Bearer → Principal（验签 + issuer/audience 校验 + jti 吊销检查）。
get_principal = build_principal_dependency(
    key_store=get_public_key_store(),
    issuer=get_settings().identity_issuer,
    audience=get_settings().identity_audience,
    revocation_check=check_request_revocation,
)

# RLS 工作单元：Principal → 注入 tenant 上下文 → UoW（写 / 只读两种）。
# 向后兼容别名（默认落到 project_planning 域会话工厂，见 database.lifespan）。
get_tenant_uow = build_tenant_uow_dependency(get_principal, get_uow)
get_read_tenant_uow = build_tenant_uow_dependency(get_principal, get_read_uow)

# 按业务域拆连接池（DATA-1）：project_planning / asset_service 各自独立
# 会话工厂 + 独立数据库角色，路由按域注入对应工作单元，使跨域越界写在
# 数据库层被拒绝。project_planning 域沿用默认别名即可，asset_service 域
# 显式绑定 asset_service 会话工厂。
get_project_tenant_uow = build_tenant_uow_dependency(
    get_principal, make_uow_dependency("project_planning_sessionmaker")
)
get_read_project_tenant_uow = build_tenant_uow_dependency(
    get_principal, make_read_uow_dependency("project_planning_sessionmaker")
)
get_asset_tenant_uow = build_tenant_uow_dependency(
    get_principal, make_uow_dependency("asset_service_sessionmaker")
)
get_read_asset_tenant_uow = build_tenant_uow_dependency(
    get_principal, make_read_uow_dependency("asset_service_sessionmaker")
)


def require_role(action_key: str) -> Callable[..., Awaitable[Principal]]:
    """构造绑定 ``get_principal`` 的角色授权依赖（所有写入口的统一授权闸门）。

    每个写路由按自身动作注入，例如 ``Depends(require_role("PROJECT.CREATE"))``。
    依赖先复用 ``get_principal`` 解析可信 Principal，再用其 roles 与
    ``action_registry[action_key].allowed_principals`` 求交，越权抛 403。
    """
    return build_authorize_dependency(get_principal, action_key)


__all__ = [
    "Principal",
    "get_asset_tenant_uow",
    "get_principal",
    "get_project_tenant_uow",
    "get_read_asset_tenant_uow",
    "get_read_project_tenant_uow",
    "get_read_tenant_uow",
    "get_tenant_uow",
    "require_role",
]
