"""身份治理依赖统一装配（供各 API 路由复用）。

职责：把「认证（Principal）+ RLS 租户上下文 + 域绑定工作单元」组装为可直接
注入 FastAPI 路由的依赖对象，避免每个路由重复拼装 KeyStore / 依赖工厂。

依赖链（单向，无循环）：
    get_principal            —— Authorization Bearer → Principal（验签 + 吊销）
    get_<domain>_uow         —— Principal → 该域连接池 → SET LOCAL 租户上下文 → 写 UoW
    get_read_<domain>_uow    —— 同上，只读工作单元（数据库层拒绝写语句）

**工作单元必须按域取用**：域决定连接池与数据库角色，角色决定能写哪些表。
用项目规划域的工作单元去写资产表会在数据库层被拒（``permission denied``），
这不是约定而是约束——跨模块写表在本项目里没有可用路径。

事务约定（R07）：写用例在返回响应前显式 ``await uow.commit()``；
不依赖 yield 依赖退出码提交（退出码在响应发出之后执行）。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import Depends, Request

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.identity.enforcement import authorize
from packages.identity.principal import Principal
from packages.identity.token_revocation import is_token_revoked
from packages.shared.config import get_settings
from packages.shared.db_roles import DatabaseDomain
from packages.shared.rls import build_tenant_uow_dependency
from services.identity_access import get_public_key_store
from services.identity_access.auth_dependency import build_principal_dependency


async def check_request_revocation(request: Request, jti: str) -> bool:
    """令牌吊销检查（固定走身份域连接池）。

    放在装配模块而不是 ``packages.identity`` 里，是为了让"取哪个连接池"这个
    决定只出现在一处：身份能力本身只提供纯查询函数，取池属于进程装配职责。

    固定走身份域：``auth.revoked_tokens`` 只授予该域角色，业务域角色无权读
    吊销表。吊销检查因此不会成为"业务域也需要 auth 权限"的破口。
    """
    pools = getattr(request.app.state, "pools", None)
    if pools is None:
        # fail-closed：无法确认吊销状态时拒绝（见 token_revocation 模块 docstring）。
        raise DomainError(
            ErrorRegistry.AUTH_UNAUTHENTICATED,
            detail="无法校验令牌吊销状态",
        )
    return await is_token_revoked(pools.sessionmaker(DatabaseDomain.IDENTITY), jti)


# 认证依赖：Bearer → Principal（验签 + issuer/audience 校验 + jti 吊销检查）。
get_principal = build_principal_dependency(
    key_store=get_public_key_store(),
    issuer=get_settings().identity_issuer,
    audience=get_settings().identity_audience,
    revocation_check=check_request_revocation,
)

# RLS 工作单元：按域绑定连接池，注入租户上下文后提供 UoW。
get_project_uow = build_tenant_uow_dependency(
    get_principal, domain=DatabaseDomain.PROJECT_PLANNING
)
get_read_project_uow = build_tenant_uow_dependency(
    get_principal, domain=DatabaseDomain.PROJECT_PLANNING, read_only=True
)
get_asset_uow = build_tenant_uow_dependency(
    get_principal, domain=DatabaseDomain.ASSET
)
get_read_asset_uow = build_tenant_uow_dependency(
    get_principal, domain=DatabaseDomain.ASSET, read_only=True
)


def require_action(action_key: str) -> Callable[..., Awaitable[Principal]]:
    """构造绑定 ``get_principal`` 的动作授权依赖（所有入口的统一授权闸门）。

    每个路由按自身动作注入，例如 ``Depends(require_action("PROJECT.CREATE"))``。
    判定不是"角色交集"，而是 ``Principal + Action + Resource + Context`` 的决策
    （见 ``packages/identity/authorization.py``）：角色、风险等级（Agent 不得过
    CRITICAL）、审批对象、组织与项目范围。

    依赖阶段拿不到具体资源，组织/项目维度由业务层的 ``authorize(..., resource=...)``
    或数据库行级策略执行；决策对象里的 ``deferred_dimensions`` 会如实记录这一点。
    依赖负责"不合法的主体进不了处理函数"，业务层负责"这个资源他能不能动"。
    """

    async def dependency(
        principal: Principal = Depends(get_principal),
    ) -> Principal:
        authorize(principal, action_key)
        return principal

    return dependency


__all__ = [
    "Principal",
    "get_asset_uow",
    "get_principal",
    "get_project_uow",
    "get_read_asset_uow",
    "get_read_project_uow",
    "require_action",
]
