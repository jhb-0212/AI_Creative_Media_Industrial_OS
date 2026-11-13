"""PostgreSQL RLS 会话上下文注入（决策 5：多租户行级安全）。

设计要点（P0-3 根治核心，R2 不变量放最可靠层级）：
- 每个数据库请求会话解析 JWT 后，把 tenant_id 注入 ``SET LOCAL
  app.current_tenant_id = '<uuid>'``，使 RLS 策略在数据库层生效。
- 研发即使写 ``SELECT * FROM projects``（不加 tenant 过滤），RLS 也会
  自动只返回当前租户数据——越权漏洞按死在数据库层，而非依赖应用层自觉。
- ``SET LOCAL`` 仅在当前事务内生效，事务结束自动清除，避免跨请求泄漏。

安全要点：
- tenant_id 来自可信 Principal（验签后的 UUID），非客户端字符串，杜绝
  SQL 注入式的 RLS 上下文污染。
- 本模块不依赖具体业务，可复用于所有走 RLS 的 schema。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable

from fastapi import Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from packages.identity.principal import Principal
from packages.shared.unit_of_work import UnitOfWork

# RLS 上下文配置键（单一事实源，与迁移中的 RLS 策略 USING 子句对齐）。
RLS_TENANT_SETTING = "app.current_tenant_id"

# 依赖函数类型别名：解析 Principal 的异步可调用对象。
PrincipalDependency = Callable[..., Awaitable[Principal]]


async def set_tenant_context(session: AsyncSession, tenant_id: object) -> None:
    """在当前事务内设置 RLS 租户上下文（SET LOCAL，事务级生效）。

    必须在同一事务的首次查询前调用；tenant_id 会作为 UUID 字符串注入，
    与 RLS 策略 ``NULLIF(current_setting('app.current_tenant_id', true), '')::uuid``
    精确匹配。
    """
    await session.execute(
        text("SELECT set_config(:key, :value, true)"),
        {"key": RLS_TENANT_SETTING, "value": str(tenant_id)},
    )


def build_tenant_uow_dependency(
    get_principal: PrincipalDependency,
    uow_dependency: Callable[..., AsyncIterator[UnitOfWork]],
) -> Callable[..., AsyncIterator[UnitOfWork]]:
    """构造 tenant-aware 工作单元依赖：解析 Principal → 注入 RLS 上下文 → 提供 UoW。

    读写共用同一装配逻辑：传 ``get_uow`` 得写单元，传 ``get_read_uow`` 得只读
    单元（数据库层拒绝写语句）。RLS 上下文与事务同生命周期，事务结束自动清除。
    """

    async def get_tenant_uow(
        principal: Principal = Depends(get_principal),
        uow: UnitOfWork = Depends(uow_dependency),
    ) -> AsyncIterator[UnitOfWork]:
        """注入 RLS 租户上下文后提供工作单元。"""
        await set_tenant_context(uow.session, principal.tenant_id)
        yield uow

    return get_tenant_uow
