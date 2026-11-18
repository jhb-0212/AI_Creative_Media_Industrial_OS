"""统一授权决策入口（RBAC + Capability 运行时真实执行）。

设计要点（F02 根治，非打补丁）：
- 此前 Principal 只用于身份解析与 RLS 租户上下文，授权判定只有一条
  ``roles & allowed`` 角色求交；而 ``action_registry.yaml`` 里 ``token_required``
  与 ``scope`` 是**死声明**（运行时零消费），Capability 从未落地。
- 本模块把授权收敛为**单一决策入口** ``authorize(action_key, principal)``，
  按固定顺序执行授权链：
    1. Action 解析（action_key 未注册 → 结构化内部错误，fail-fast）。
    2. RBAC：``Principal.roles`` 与 ``allowed_principals`` 的**会签席位**判定
       （单席位 = 任一候选角色；多席位 = 要求多方会签，当前显式拒绝）。
    3. Capability：契约声明 ``token_required: true`` 的动作，要求 Principal
       携带覆盖该动作的能力范围（caps claim），否则拒绝。
  后续 ABAC / 资源范围 / 职责分离在同一入口内追加，而非在各业务模块
  各自散落 if 判断（红线：禁止新增平行授权层）。
- fail-closed：未注册 action_key、空授权、角色不匹配、能力缺失，一律拒绝。
- 依赖工厂与认证依赖解耦（传入 ``get_principal``），保持单向依赖、无循环。

Capability 的语义边界（与 RBAC 严格区分）：
- RBAC 回答「这个主体属于什么角色，能执行什么动作类别」。
- Capability 回答「这个主体被授予了哪些具体资源/动作范围」——它是
  短效、可范围限定、随 token 携带的授权凭证。
- 一个动作若 ``token_required: true``，则两者都要满足；若 false，仅 RBAC
  即可（Capability 不做强制，保持对既有低风险读/写动作的向后兼容）。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import Depends

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.identity.ownership import (
    OwnershipPath,
    ResourceRef,
    assert_ownership_scope,
)
from packages.identity.principal import Principal
from services.identity_access.action_registry import (
    action_definition,
    principal_seats,
)

# 认证依赖函数类型别名：解析出 Principal 的异步可调用对象。
PrincipalDependency = Callable[..., Awaitable[Principal]]


def authorize(
    action_key: str,
    principal: Principal,
    resource: ResourceRef | None = None,
    ownership: OwnershipPath | None = None,
) -> Principal:
    """统一授权决策入口（同步纯函数，供依赖层与单元测试直接调用）。

    授权链：Action 解析 → RBAC → Capability → **资源归属范围**。
    任一步不满足即拒绝（动作级 403 / 资源级 404 或 403，语义见
    ``packages/identity/ownership.py``），全部通过则原样返回 Principal。

    为什么资源参数在这里而不是在路由里（第二轮审查 P1-03/P1-04 的根因）：
    此前签名只有 ``(action_key, principal)``，决策函数无法回答「能不能碰这个
    对象」，资源级判定只能长在调用点上——project 手写了一段、episode 没有、
    asset 写不了。把资源作为入参是本轮唯一的修法；在三个路由各补一段判断，
    第四个资源还会漏。

    职责边界：
    - 归属**解析**（查库）由调用方在异步边界完成并传入 ``ownership``，
      本函数保持同步纯函数（可单测、无 IO）；
    - 范围**判定**在本函数内完成（``assert_ownership_scope``），
      业务模块不接触该规则。

    此函数是唯一的运行时授权判定点——业务路由不得再自行实现角色/能力/归属比对
    （红线：单一事实源 + 单一执行责任）。
    """
    definition = action_definition(action_key)

    # 1. RBAC：按「会签席位」判定（allowed_principals 为空视为契约配置错误）。
    #
    # 为什么不是「角色求交」了：契约里 `+` 表达的是「这些席位必须由不同主体
    # 分别签署」，`/` 才表达「同一席位任选其一」。旧实现只按 `/` 切分，把
    # `交付负责人+内部 QC` 当成一个角色名——那个角色永远不存在，于是判定的
    # 结果永远是否决，原因却是「缺少角色」，掩盖了「这是一条双人会签要求」。
    seats = principal_seats(action_key)
    if not seats:
        raise DomainError(
            ErrorRegistry.AUTH_SCOPE_DENIED,
            detail=f"动作 {action_key} 未配置允许角色",
        )
    if len(seats) > 1:
        # 多席位 = 会签：需要 N 个不同真实主体各签一次，而单次请求只携带一个
        # Principal。此处必须显式拒绝并说明「是能力未实现」，不能退化成
        # 「某个角色碰巧匹配就放行」——那等于把会签要求静默降级成单人授权。
        if not any(principal.roles & seat for seat in seats):
            raise DomainError(
                ErrorRegistry.AUTH_SCOPE_DENIED,
                detail=f"缺少执行 {action_key} 所需角色",
            )
        required = " + ".join("/".join(sorted(seat)) for seat in seats)
        raise DomainError(
            ErrorRegistry.AUTH_SCOPE_DENIED,
            detail=(
                f"动作 {action_key} 要求 {len(seats)} 方会签（{required}），"
                "须由不同主体分别签署；当前授权链不支持多主体累积审批"
            ),
        )
    if not (principal.roles & seats[0]):
        raise DomainError(
            ErrorRegistry.AUTH_SCOPE_DENIED,
            detail=f"缺少执行 {action_key} 所需角色",
        )

    # 2. Capability：契约声明 token_required 的动作，要求能力范围覆盖该动作。
    if definition.token_required and not _capability_covers(definition, principal):
        raise DomainError(
            ErrorRegistry.AUTH_SCOPE_DENIED,
            detail=f"缺少执行 {action_key} 所需的能力范围（Capability Token）",
        )

    # 3. 资源归属范围：动作级通过后，才判定「这个对象归不归你」。
    if resource is not None:
        assert_ownership_scope(principal, resource, ownership)

    return principal


def _capability_covers(definition: object, principal: Principal) -> bool:
    """判定 Principal 的能力范围是否覆盖指定动作（fail-closed）。

    能力范围以 ``caps`` claim 的字符串集合表达；匹配约定：
    - 精确匹配 ``action_key``（如 ``PROJECT.CREATE``）。
    - 或通配 ``resource_type:*``（如 ``Project:*`` 覆盖该资源下所有动作）。
    - 或顶层通配 ``*``（覆盖全部动作，仅应授予受信的内部服务账号）。

    三者都不命中即视为未覆盖（fail-closed，绝不静默放行）。
    """
    from services.identity_access.action_registry import ActionDefinition

    assert isinstance(definition, ActionDefinition)  # 类型守卫，防误用
    action_key = definition.action_key
    resource_type = definition.resource_type
    wildcard_resource = f"{resource_type}:*" if resource_type else None
    return (
        action_key in principal.capabilities
        or (wildcard_resource is not None and wildcard_resource in principal.capabilities)
        or "*" in principal.capabilities
    )


def build_authorize_dependency(
    get_principal: PrincipalDependency,
    action_key: str,
) -> Callable[..., Awaitable[Principal]]:
    """构造指定 action 的统一授权依赖（供 ``Depends(...)`` 注入）。

    依赖链：``authorize -> get_principal``（复用认证依赖，FastAPI 缓存去重）。
    判定：调用 :func:`authorize` 执行 RBAC → Capability 授权链，
    通过则原样返回 Principal，否则抛 403 ``AUTH_SCOPE_DENIED``。
    """

    async def _authorize(
        principal: Principal = Depends(get_principal),
    ) -> Principal:
        return authorize(action_key, principal)

    return _authorize
