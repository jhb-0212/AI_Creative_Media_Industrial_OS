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

from collections.abc import AsyncIterator, Awaitable, Callable

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from packages.identity.principal import Principal
from packages.identity.token_revocation import check_request_revocation
from packages.shared.config import get_settings
from packages.shared.rls import build_tenant_uow_dependency
from packages.shared.unit_of_work import (
    UnitOfWork,
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

# R16 三域拆分（a0b1c2d3e4f5）：生产链 / 预算额度 / 质检治理各自一个连接池。
# 路由注入哪一域，决定了它**在这一路上**用的是哪个数据库角色——越域写不靠
# 代码审查拦，靠 permission denied 拦。
get_production_planning_tenant_uow = build_tenant_uow_dependency(
    get_principal, make_uow_dependency("production_planning_sessionmaker")
)
get_read_production_planning_tenant_uow = build_tenant_uow_dependency(
    get_principal, make_read_uow_dependency("production_planning_sessionmaker")
)
get_budget_quota_tenant_uow = build_tenant_uow_dependency(
    get_principal, make_uow_dependency("budget_quota_sessionmaker")
)
get_read_budget_quota_tenant_uow = build_tenant_uow_dependency(
    get_principal, make_read_uow_dependency("budget_quota_sessionmaker")
)
get_qc_governance_tenant_uow = build_tenant_uow_dependency(
    get_principal, make_uow_dependency("qc_governance_sessionmaker")
)
get_read_qc_governance_tenant_uow = build_tenant_uow_dependency(
    get_principal, make_read_uow_dependency("qc_governance_sessionmaker")
)

# D6-9 运维重放：独立连接池 + 独立角色（outbox_ops_role）。它不是业务域
# UoW——没有自有 schema，只对 outbox 有「读事件 + 清死信三列 + 写审计表」的
# 最小权限。因此这里只提供写单元：重放必须写审计，只读池在数据库层就会被拒。
get_outbox_ops_tenant_uow = build_tenant_uow_dependency(
    get_principal, make_uow_dependency("outbox_ops_sessionmaker")
)


def require_role(action_key: str) -> Callable[..., Awaitable[Principal]]:
    """构造绑定 ``get_principal`` 的角色授权依赖（动作级闸门）。

    用于「被操作资源尚不存在」或「无受控资源」的动作（如健康检查旁路）。
    一旦动作作用于某个受控资源，必须改用 :func:`require_resource_role`——
    动作级通过只说明「你能做这类事」，不说明「这个对象归你」。
    """
    return build_authorize_dependency(get_principal, action_key)


def require_resource_role(
    action_key: str,
    *,
    resource_type: str,
    param_name: str,
    uow_dep: Callable[..., AsyncIterator[UnitOfWork]],
) -> Callable[..., Awaitable[Principal]]:
    """资源级授权依赖：动作级判定 + 归属范围判定，一次完成。

    ``param_name`` 是路径参数名（``entity_id`` / ``asset_version_id``）。
    归属解析在依赖内完成（异步 IO），判定仍在 ``authorize`` 内（单一入口），
    路由层因此不需要、也不允许再写任何归属判断。
    """
    from packages.identity.ownership import ResourceRef, SqlOwnershipResolver
    from services.identity_access.rbac import authorize

    async def _dep(
        request: Request,
        principal: Principal = Depends(get_principal),
        uow: UnitOfWork = Depends(uow_dep),
    ) -> Principal:
        resource_id = request.path_params.get(param_name)
        ownership = (
            await SqlOwnershipResolver(uow.session).resolve(resource_type, resource_id)
            if resource_id is not None
            else None
        )
        return authorize(
            action_key,
            principal,
            ResourceRef(resource_type, resource_id),
            ownership,
        )

    return _dep


async def authorize_on_resource(
    action_key: str,
    principal: Principal,
    session: AsyncSession,
    *,
    resource_type: str,
    resource_id: str,
) -> Principal:
    """对已存在资源做动作级 + 资源级授权（请求体里的 id 也走同一入口）。

    ``require_resource_role`` 只能读路径参数；镜头修订确认的 shot_id 在请求体
    里。让路由自己解析/判定会把 AUTH-1 的资源输入散到调用点，因此提供这个函数，
    判定仍统一落在 ``authorize``，解析仍统一落在 ``SqlOwnershipResolver``。
    """
    from packages.identity.ownership import ResourceRef, SqlOwnershipResolver
    from services.identity_access.rbac import authorize

    ownership = await SqlOwnershipResolver(session).resolve(resource_type, resource_id)
    return authorize(
        action_key,
        principal,
        ResourceRef(resource_type, resource_id),
        ownership,
    )


async def authorize_on_parent(
    action_key: str,
    principal: Principal,
    session: AsyncSession,
    *,
    parent_type: str,
    parent_id: str,
) -> Principal:
    """创建类动作的授权：判定主体能否在给定父资源下新建子资源。

    子资源尚不存在，因此把父资源（项目）的归属作为判定对象——创建集数、
    登记资产都要先证明「这个项目归你」，否则可以先在别人项目里造数据，
    再借后续流程读取。
    """
    from packages.identity.ownership import ResourceRef, SqlOwnershipResolver
    from services.identity_access.rbac import authorize

    ownership = await SqlOwnershipResolver(session).resolve(parent_type, parent_id)
    return authorize(action_key, principal, ResourceRef(parent_type, parent_id), ownership)


__all__ = [
    "Principal",
    "authorize_on_parent",
    "authorize_on_resource",
    "get_asset_tenant_uow",
    "get_budget_quota_tenant_uow",
    "get_outbox_ops_tenant_uow",
    "get_principal",
    "get_production_planning_tenant_uow",
    "get_project_tenant_uow",
    "get_qc_governance_tenant_uow",
    "get_read_asset_tenant_uow",
    "get_read_budget_quota_tenant_uow",
    "get_read_production_planning_tenant_uow",
    "get_read_project_tenant_uow",
    "get_read_qc_governance_tenant_uow",
    "get_read_tenant_uow",
    "get_tenant_uow",
    "require_resource_role",
    "require_role",
]
