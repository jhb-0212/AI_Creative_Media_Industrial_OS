"""PostgreSQL RLS 会话上下文注入与「RLS 是否真的生效」的启动期验证。

设计要点（P0-3 根治核心，R2 不变量放最可靠层级）
----------------------------------------------
- 每个数据库请求会话解析 JWT 后，把作用域写进会话：``SET LOCAL
  app.current_tenant_id`` / ``app.current_organization_id``，使 RLS 策略在
  数据库层生效。
- 研发即使写 ``SELECT * FROM projects``（不加任何过滤），RLS 也会只返回
  当前租户 + 当前组织范围内可见的行——越权按死在数据库层，而非依赖应用层自觉。
- ``SET LOCAL`` 仅在当前事务内生效，事务结束自动清除，避免跨请求泄漏。

为什么组织维度必须和租户维度在**同一处**注入（ADR-R24）
------------------------------------------------------
此前本模块只注入租户。缺一个维度时，数据库只能写出一个维度的策略——写十条也一样，
组织边界 100% 由应用层判定。新增一个可注入的维度，必须在同一个装配点完成，
否则「有的入口注入了、有的没注入」会变成新的静默缺口：没注入的入口看起来正常，
只是恰好只能看到个人租户的数据。

安全要点
--------
- 租户/组织 id 来自可信 Principal（验签后的 UUID），非客户端字符串，杜绝
  SQL 注入式的上下文污染。
- 本模块不依赖具体业务，可复用于所有走 RLS 的 schema。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass

from fastapi import Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from packages.identity.principal import Principal
from packages.shared.logging import get_logger
from packages.shared.unit_of_work import UnitOfWork

logger = get_logger(__name__)

# RLS 上下文配置键（单一事实源，与迁移中的 RLS 策略 USING 子句对齐）。
RLS_TENANT_SETTING = "app.current_tenant_id"
RLS_ORGANIZATION_SETTING = "app.current_organization_id"

# 依赖函数类型别名：解析 Principal 的异步可调用对象。
PrincipalDependency = Callable[..., Awaitable[Principal]]


def tenant_scope_predicate(column: str = "tenant_id") -> str:
    """租户谓词（SQL 片段）：未注入上下文时恒为 false（fail-closed）。

    ``NULLIF(current_setting(...), '')`` 把「未定义」与「空串」都归一为 NULL，
    避免 ``''::uuid`` 转换错误；``x = NULL`` 恒为 NULL → 无行可见。
    """
    return f"{column} = NULLIF(current_setting('{RLS_TENANT_SETTING}', true), '')::uuid"


def organization_scope_predicate(column: str = "organization_id") -> str:
    """组织谓词（SQL 片段），与应用层 ``assert_ownership_scope`` 逐格等价。

    真值表（``organization_id`` 指资源列，``current_org`` 指会话里的当前组织）：

    | 资源组织 | 主体有无组织 | 谓词结果 |
    | --- | --- | --- |
    | NULL（个人租户） | 有无皆可 | 真（可见） |
    | 非空 | 同为该组织 | 真 |
    | 非空 | 无组织 / 组织不同 | 假（不可见，含 NULL 比较不成立） |

    为什么由函数生成而不是在各个迁移里手抄：这条规则一变，抄了 N 处就有 N 处要改，
    漏一处即出现「应用放行、数据库拒绝」或反过来的两套规则（ADR-R24 §D3a-2）。
    """
    return (
        f"({column} IS NULL OR {column} = "
        f"NULLIF(current_setting('{RLS_ORGANIZATION_SETTING}', true), '')::uuid)"
    )


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


async def set_organization_context(session: AsyncSession, organization_id: object | None) -> None:
    """在当前事务内设置 RLS 组织上下文（SET LOCAL）。

    ``organization_id`` 为 None 时显式写入空串：``NULLIF(...,'')`` 归一为 NULL，
    于是策略只剩「个人租户行可见」——与「无 org claim 的主体不得访问组织资源」
    一致（fail-closed，而不是「不设置就等于不限」）。
    """
    await session.execute(
        text("SELECT set_config(:key, :value, true)"),
        {
            "key": RLS_ORGANIZATION_SETTING,
            "value": "" if organization_id is None else str(organization_id),
        },
    )


async def set_session_scope(
    session: AsyncSession,
    *,
    tenant_id: object,
    organization_id: object | None,
) -> None:
    """一次写入两个作用域维度（缓存作用域的唯一注入点）。"""
    await set_tenant_context(session, tenant_id)
    await set_organization_context(session, organization_id)


def build_tenant_uow_dependency(
    get_principal: PrincipalDependency,
    uow_dependency: Callable[..., AsyncIterator[UnitOfWork]],
) -> Callable[..., AsyncIterator[UnitOfWork]]:
    """构造 scoped 工作单元依赖：解析 Principal → 注入 RLS 上下文 → 提供 UoW。

    读写共用同一装配逻辑：传 ``get_uow`` 得写单元，传 ``get_read_uow`` 得只读
    单元（数据库层拒绝写语句）。RLS 上下文与事务同生命周期，事务结束自动清除。

    名称里的 "tenant" 是历史叫法，现在承载「租户 + 组织」两个维度；不改名是因为
    六个别名的调用点全在这里，改名只增加 diff、不改变任何行为（ADR-R24 §D3a-1）。
    """

    async def get_tenant_uow(
        principal: Principal = Depends(get_principal),
        uow: UnitOfWork = Depends(uow_dependency),
    ) -> AsyncIterator[UnitOfWork]:
        """注入 RLS 作用域上下文后提供工作单元。"""
        await set_session_scope(
            uow.session,
            tenant_id=principal.tenant_id,
            organization_id=principal.organization_id,
        )
        yield uow

    return get_tenant_uow


# --------------------------------------------------------------------------- #
# 启动期验证：RLS 到底会不会生效
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RlsEnforcement:
    """一次 RLS 生效性探测的结果（启动期判定用）。"""

    role: str
    is_superuser: bool
    bypass_rls: bool
    rls_enabled_tables: int
    rls_disabled_tables: tuple[str, ...]
    owned_tables_without_force: tuple[str, ...]

    @property
    def overrides_rls(self) -> bool:
        """该角色是否会整体绕过 RLS（超级用户 / BYPASSRLS）。"""
        return self.is_superuser or self.bypass_rls

    def problems(self) -> list[str]:
        """返回全部生效性问题（空列表表示「RLS 对该连接真实生效」）。"""
        issues: list[str] = []
        if self.is_superuser:
            issues.append(f"连接角色 {self.role} 是超级用户：行级安全整体失效（含 FORCE）")
        if self.bypass_rls:
            issues.append(f"连接角色 {self.role} 带 BYPASSRLS：行级安全整体失效")
        if self.rls_disabled_tables:
            issues.append(f"以下表未启用 RLS：{list(self.rls_disabled_tables)}")
        if self.owned_tables_without_force:
            issues.append(
                "以下表的当前角色是所有者且未 FORCE RLS：查询会绕过策略 "
                f"{list(self.owned_tables_without_force)}"
            )
        return issues


# 启动期检查的观测表：取每个业务 schema 的一张策略表。
# 为什么不是「全部表」：启动期只回答「RLS 会不会生效」，不做 schema 审计——
# 全表扫描会把启动耗时绑到表数量上，而「有几张表没开 RLS」由契约闸门负责。
_RLS_PROBE_TABLES: tuple[tuple[str, str], ...] = (
    ("project_planning", "projects"),
    ("asset_service", "asset_versions"),
    ("audit", "status_transitions"),
)

_PROBE_SQL = text(
    """
    SELECT
        current_user AS role,
        (SELECT rolsuper FROM pg_roles WHERE rolname = current_user) AS is_superuser,
        (SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user) AS bypass_rls
    """
)

_TABLE_SQL = text(
    """
    SELECT
        n.nspname AS schema_name,
        c.relname AS table_name,
        c.relrowsecurity AS rls_enabled,
        c.relforcerowsecurity AS rls_forced,
        pg_get_userbyid(c.relowner) AS owner
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = :schema AND c.relname = :table
    """
)


async def probe_rls_enforcement(engine: AsyncEngine) -> RlsEnforcement:
    """用**业务连接**探测 RLS 是否生效（不在猜测「权限看起来都通」）。"""
    async with engine.connect() as conn:
        row = (await conn.execute(_PROBE_SQL)).one()
        disabled: list[str] = []
        owned_without_force: list[str] = []
        enabled = 0
        for schema, table in _RLS_PROBE_TABLES:
            info = (await conn.execute(_TABLE_SQL, {"schema": schema, "table": table})).first()
            if info is None:
                continue
            if not info.rls_enabled:
                disabled.append(f"{schema}.{table}")
                continue
            enabled += 1
            if info.owner == row.role and not info.rls_forced:
                owned_without_force.append(f"{schema}.{table}")
    return RlsEnforcement(
        role=str(row.role),
        is_superuser=bool(row.is_superuser),
        bypass_rls=bool(row.bypass_rls),
        rls_enabled_tables=enabled,
        rls_disabled_tables=tuple(disabled),
        owned_tables_without_force=tuple(owned_without_force),
    )


async def verify_rls_enforcement(engine: AsyncEngine, *, app_env: str) -> None:
    """启动期验证：RLS 对这条连接真实生效，否则生产环境拒绝启动。

    为什么在启动期而不是每请求：运行期发现「安全属性未知」已经太晚——请求在跑。
    为什么生产硬失败、非生产只告警：本地/CI 用 ``postgres`` 连库是既有事实，
    把它变成硬失败会让开发无法启动；但这条差异必须写在代码里，并且**生产路径
    上不存在任何绕过**（测试覆盖「生产 + 超级用户 → 抛错」）。

    生产环境的硬失败同时是「DSN 误配」的兜底：把业务进程指向 ``postgres``
    会让全部 RLS 静默失效，而这种误配在权限检查上看起来完全正常。
    """
    enforcement = await probe_rls_enforcement(engine)
    problems = enforcement.problems()
    if not problems:
        return
    message = f"RLS 未对连接角色 {enforcement.role} 生效：" + "；".join(problems)
    if app_env == "production":
        raise RuntimeError(message + "。生产环境拒绝以该角色启动业务进程。")
    # 关键字参数就是字段：structlog 不接受 stdlib 的 extra={...}，
    # 写成 extra 会把整包字段压进一个 "extra" 键，日志检索按字段筛就筛不到。
    logger.warning(
        "rls_enforcement_degraded",
        role=enforcement.role,
        problems=problems,
        app_env=app_env,
    )


__all__ = [
    "RLS_ORGANIZATION_SETTING",
    "RLS_TENANT_SETTING",
    "RlsEnforcement",
    "build_tenant_uow_dependency",
    "organization_scope_predicate",
    "probe_rls_enforcement",
    "set_organization_context",
    "set_session_scope",
    "set_tenant_context",
    "tenant_scope_predicate",
    "verify_rls_enforcement",
]
