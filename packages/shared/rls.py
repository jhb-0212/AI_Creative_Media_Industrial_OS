"""PostgreSQL RLS 会话上下文注入（决策 5：多租户行级安全 + 项目级隔离）。

设计要点（P0-3 根治核心，R2 不变量放最可靠层级）：
- 每个数据库请求会话解析 JWT 后，把租户与项目范围注入事务级 GUC：
  ``app.current_tenant_id``（哪家客户）与 ``app.current_projects``（客户内
  哪些项目），使两条 RLS 策略在数据库层同时生效。
- 研发即使写 ``SELECT * FROM projects``（不加任何过滤），RLS 也会自动只返回
  当前租户、当前项目范围的数据——越权漏洞按死在数据库层，而非依赖应用层自觉。
- ``SET LOCAL``（``set_config(..., true)``）仅在当前事务内生效，事务结束自动
  清除，避免跨请求泄漏。

为什么项目范围也放数据库层：
- 「每个 handler 记得校验一次」是最容易漏的形态：新增一个路由就多一个漏点，
  而且漏了不会有任何症状。放数据库层只有一处开关，新路由天然受约束。
- 与租户隔离共用同一条注入路径，也让「谁能看见哪些数据」只有一份定义。

安全要点：
- 两个 GUC 的取值都来自可信 Principal（验签后的 UUID / 已签发范围），
  非客户端字符串，杜绝 SQL 注入式的 RLS 上下文污染。
- 空范围（``project_ids`` 为空集）注入空串，策略判否：一个项目都看不见。
  不写"没声明就放行"的默认值——那正是历史上最常见的越权来源。
- 本模块不依赖具体业务，可复用于所有走 RLS 的 schema。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable

from fastapi import Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.identity.principal import PROJECT_SCOPE_ALL, Principal
from packages.shared.db_roles import DatabaseDomain
from packages.shared.unit_of_work import UnitOfWork, build_domain_uow_dependency

# RLS 上下文配置键（单一事实源，与迁移中的 RLS 策略 USING 子句对齐）。
RLS_TENANT_SETTING = "app.current_tenant_id"
RLS_ORG_SETTING = "app.current_org_id"
RLS_PROJECT_SETTING = "app.current_projects"

# 项目 id 列表的分隔符。取值来自签名令牌，正常不会含逗号；含逗号会让一个 id
# 被拆成两个（等于扩大授权），因此在唯一写入点直接拒绝，而不是在策略里做兼容。
_PROJECT_SCOPE_SEPARATOR = ","

# 依赖函数类型别名：解析 Principal 的异步可调用对象。
PrincipalDependency = Callable[..., Awaitable[Principal]]


async def set_tenant_context(session: AsyncSession, tenant_id: object) -> None:
    """在当前事务内设置 RLS 租户上下文（SET LOCAL，事务级生效）。

    必须在同一事务的首次查询前调用；tenant_id 会作为 UUID 字符串注入，
    与 RLS 策略 ``NULLIF(current_setting('app.current_tenant_id', true), '')::uuid``
    精确匹配。

    仅设置租户（不设项目范围）的调用方只应访问**不带项目归属**的表
    （如 outbox 事件表）；访问项目级资源表会因项目范围为空而看不到任何行。
    """
    await session.execute(
        text("SELECT set_config(:key, :value, true)"),
        {"key": RLS_TENANT_SETTING, "value": str(tenant_id)},
    )


def encode_project_scope(project_ids: frozenset[str]) -> str:
    """把 Principal 的项目范围编码为 RLS GUC 取值（唯一编码实现）。

    - 持有本组织内全部项目范围（含 ``*``）→ 字面量 ``*``；
    - 非空集合 → 逗号分隔；
    - 空集 → 空串（策略判否，看不见任何项目）。
    """
    if PROJECT_SCOPE_ALL in project_ids:
        return PROJECT_SCOPE_ALL
    for project_id in project_ids:
        if _PROJECT_SCOPE_SEPARATOR in project_id or not project_id.strip():
            # 编码不变量在唯一写入点强制：否则一个 id 会被拆成多个，
            # 或空 id 与"未设置"混同。
            raise DomainError(
                ErrorRegistry.AUTH_UNAUTHENTICATED,
                detail="令牌的项目范围含非法项目标识",
            )
    return _PROJECT_SCOPE_SEPARATOR.join(sorted(project_ids))


async def set_request_context(session: AsyncSession, principal: Principal) -> None:
    """注入请求的数据访问上下文：租户 + 组织 + 项目范围（同一事务内一次完成）。

    三步都必须在同一事务的首次查询前完成；任一缺失都会让 RLS 判否
    （fail-closed），因此这里不做"先设租户、项目稍后补"的分步调用。

    三个 GUC 分别对应三层边界：tenant（哪家客户）、organization（客户内部
    哪个制作主体）、projects（哪些项目）。层级从粗到细，缺任何一层都会让
    数据在"该看不见的时候被看见"。
    """
    await set_tenant_context(session, principal.tenant_id)
    await session.execute(
        text("SELECT set_config(:key, :value, true)"),
        {"key": RLS_ORG_SETTING, "value": str(principal.organization_id)},
    )
    await session.execute(
        text("SELECT set_config(:key, :value, true)"),
        {
            "key": RLS_PROJECT_SETTING,
            "value": encode_project_scope(principal.project_ids),
        },
    )


def build_tenant_uow_dependency(
    get_principal: PrincipalDependency,
    *,
    domain: DatabaseDomain,
    read_only: bool = False,
) -> Callable[..., AsyncIterator[UnitOfWork]]:
    """构造 tenant-aware 工作单元依赖：解析 Principal → 注入 RLS 上下文 → 提供 UoW。

    域是必填参数：它决定连接池与数据库角色（见 ``packages.shared.db_roles``），
    读写由 ``read_only`` 区分（数据库层拒绝写语句）。RLS 上下文与事务同生命
    周期，事务结束自动清除。
    """
    uow_dependency = build_domain_uow_dependency(domain, read_only=read_only)

    async def get_tenant_uow(
        principal: Principal = Depends(get_principal),
        uow: UnitOfWork = Depends(uow_dependency),
    ) -> AsyncIterator[UnitOfWork]:
        """注入 RLS 上下文（租户 + 项目范围）后提供工作单元。"""
        await set_request_context(uow.session, principal)
        yield uow

    return get_tenant_uow
