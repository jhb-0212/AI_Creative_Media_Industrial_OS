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
from functools import lru_cache

from fastapi import Depends, Request

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.identity.enforcement import authorize
from packages.identity.jwks import PublicKeyStore
from packages.identity.principal import Principal
from packages.identity.token_revocation import is_token_revoked
from packages.shared.config import get_settings
from packages.shared.db_roles import DatabaseDomain
from packages.shared.rls import build_tenant_uow_dependency
from services.identity_access import get_public_key_store
from services.identity_access.auth_dependency import (
    build_principal_dependency,
    extract_bearer_token,
)


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


@lru_cache(maxsize=1)
def _principal_authenticator() -> Callable[..., Awaitable[Principal]]:
    """构造认证器（惰性、进程内单例）。

    为什么不在模块导入时构造：导入期构造意味着**任何 import 本模块的行为**
    都要求进程备齐验签公钥——OpenAPI 导出、契约一致性检查、只跑路由表单测
    都会因为一个与它们无关的密钥配置而失败。密钥缺失的正确失败点是
    **进程启动**（见 ``apps.api.main`` 的 lifespan），不是 import。

    惰性构造不降低安全性：``get_public_key_store()`` 仍然 fail-closed，
    启动时会被显式调用一次（缺公钥即拒绝启动），测试不跑 lifespan 也会在
    首个请求时失败。区别只是失败点更准确，且不误伤不需要认证的用法。
    """
    settings = get_settings()
    return build_principal_dependency(
        key_store=get_public_key_store(),
        issuer=settings.identity_issuer,
        audience=settings.identity_audience,
        revocation_check=check_request_revocation,
    )


async def get_principal(
    request: Request,
    token: str = Depends(extract_bearer_token),
) -> Principal:
    """认证依赖：Bearer → Principal（验签 + issuer/audience 校验 + jti 吊销检查）。"""
    return await _principal_authenticator()(request, token)


def reload_verification_keys_if_file_changed() -> bool:
    """重读 JWKS 文件：文档变化时整体替换验签公钥与认证器（密钥轮换闭环，ADR-R29）。

    轮换闭环的另一半：签发端把新公钥追加进 JWKS 文件（多 kid 共存）后，
    验签进程在下一个轮询周期内拾取，无需重启——旧 kid 在共存期内仍可验签，
    旧令牌自然过期后签发端再移除旧 kid，完成一次无停机轮换。

    前提条件（任一不满足即返回 False，不做任何事）：

    - ``IDENTITY_PUBLIC_KEY_PEM`` 为空：PEM 来自环境注入，环境级轮换本来就
      伴随进程重启；同时配置两种来源时热刷新会制造"文件盖过 PEM"的第二条
      公钥来源，破坏 ``get_public_key_store`` 的来源优先级语义。
    - ``IDENTITY_JWKS_FILE`` 已配置：没有文件就没有可重读的轮换载体。

    失败语义（fail-closed 且无损）：重读或解析失败时异常原样抛出——此时
    **缓存尚未清空**，验签继续用旧公钥，行为等同"本轮轮换未发生"。调用方
    （刷新循环）负责记录日志，绝不静默吞掉。

    两个缓存必须同时失效：``get_public_key_store``（公钥仓储）与
    ``_principal_authenticator``（捕获了公钥仓储的认证器）。只清前者会让
    认证器继续持有旧仓储引用，轮换等于没发生——这是本函数存在的核心原因。
    """
    settings = get_settings()
    if settings.identity_public_key_pem.get_secret_value().strip():
        return False
    jwks_file = settings.identity_jwks_file.strip()
    if not jwks_file:
        return False

    # 先在缓存外构建候选仓储：文件损坏 / 格式非法在这里抛出，
    # 不会影响任何已生效的验签状态。
    candidate = PublicKeyStore.from_jwks_file(jwks_file)
    if candidate.jwks() == get_public_key_store().jwks():
        return False

    get_public_key_store.cache_clear()
    _principal_authenticator.cache_clear()
    return True

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
# 镜头规划域（Shot/ShotDraft/ShotRevision）：独立角色 svc_shot_planning。
#
# 为什么不复用项目规划域工作单元：ADR-R27 把镜头域拆为独立数据所有权，
# 项目域角色写不了镜头表、镜头域角色写不了项目表（越权写入数据库层被拒）。
get_shot_uow = build_tenant_uow_dependency(
    get_principal, domain=DatabaseDomain.SHOT_PLANNING
)
get_read_shot_uow = build_tenant_uow_dependency(
    get_principal, domain=DatabaseDomain.SHOT_PLANNING, read_only=True
)
# 质量治理域（Gate 裁决）：独立角色 svc_qc_governance。
#
# 为什么 Gate 不复用资产域工作单元：资产域角色代表"生成/检测流水线"，
# 若它能写 gate_decisions，「Agent 不得自批自用」在数据库层就只剩应用代码
# 一道防线。让裁决写入必须换一个数据库身份，是把这条红线变成结构约束。
get_qc_uow = build_tenant_uow_dependency(
    get_principal, domain=DatabaseDomain.QC_GOVERNANCE
)
get_read_qc_uow = build_tenant_uow_dependency(
    get_principal, domain=DatabaseDomain.QC_GOVERNANCE, read_only=True
)
# 通用审批引擎（services.workflow）：独立角色，业务域无审批表写入权。
get_workflow_uow = build_tenant_uow_dependency(
    get_principal, domain=DatabaseDomain.WORKFLOW
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
    "get_qc_uow",
    "get_read_asset_uow",
    "get_read_project_uow",
    "get_read_qc_uow",
    "get_read_shot_uow",
    "get_shot_uow",
    "get_workflow_uow",
    "reload_verification_keys_if_file_changed",
    "require_action",
]
