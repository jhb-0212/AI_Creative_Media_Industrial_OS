"""资源归属解析与范围判定（统一授权链的「资源」那一半）。

为什么需要这个模块（根因）
------------------------------------------------------------
整改前 ``authorize(action_key, principal)`` 的签名里**没有资源**，于是授权入口
无法回答「这个主体能不能碰这个对象」。资源级判定只能溢出到调用点：

* ``project_routes.py`` 自己写了一段 ``_assert_organization_scope``；
* ``episode_routes.py`` 读写两条路径都没有（第二轮审查 P1-03）；
* 资产域根本写不了——模型里没有归属字段（P1-04）。

这不是三处分别偷懒，而是接口形状决定的：决策函数拿不到资源，判定就必然长在
调用点上。本模块把「资源 → 归属路径」收敛为一处，让授权入口重新拥有完整输入。

两个概念必须分清（这是设计的关键取舍）
------------------------------------------------------------
1. **归属（ownership）**：资源在「租户 → 组织 → 项目」中的位置，是数据库里的
   事实，由外键保证（SCOPE-1/2/3）。只有一条路径，不冗余存储。
2. **作用域（scope）**：契约 ``action_registry.yaml`` 里的 ``scope: project/shot/asset``
   描述的是**授权作用域**，不是归属层级。把作用域当归属，正是 P1-04 的由来
   （于是有人想给资产加 organization_id）。本模块只解析归属，不解释 scope 文本。

响应语义（ADR-R24 修订后定死，不再逐处决定）
------------------------------------------------------------
* 资源不存在 / 不在当前作用域 → ``NOT_FOUND``：组织边界已下沉到数据库
  （``e9f0a1b2c3d4`` 的组织谓词），解析器**只能**返回当前作用域可见的行，
  因此「他组织资源」与「不存在的资源」在解析阶段就无法区分，两种情形同形。
* 同租户跨组织 → ``AUTH_SCOPE_DENIED``：这条判定仍保留，但要说清它现在的地位。
  在现有调用路径上它**恒不触发**——归属路径要么由解析器给出（受策略约束，
  必然同组织），要么是创建路径的空组织。保留它是 fail-closed：将来若有人绕过
  解析器、拿请求体直接拼出归属路径（整改前 ``_assert_organization_scope`` 正是
  这么写的），它仍会拒绝。
  也就是说：**规则只裁决一次（数据库策略），应用层这条是等价性断言，不是第二
  套规则**；两者的裁决一致性由 ``tests/integration/test_org_scope_parity.py``
  用同一张判定矩阵分别打两层来守。响应形态不同（404 不可见 / 403 显式拒绝）
  是刻意的：能走到 403 就说明调用方确实持有该资源地址，不必再隐藏存在性。

禁止事项
------------------------------------------------------------
* 不在这里做角色/能力判定——那是 ``rbac.authorize`` 的职责，本模块只产出归属事实。
* 不按 resource_type 拼 SQL：每种资源的解析语句是显式登记的一条，避免把契约里的
  字符串变成 SQL 片段（注入面）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.identity.principal import Principal


# --------------------------------------------------------------------------- #
# 值对象
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ResourceRef:
    """被访问资源的引用（类型 + 标识）。

    ``resource_id`` 为 None 表示「尚未落库的目标资源」（如创建请求里待写入的
    组织归属）——此时调用方直接构造 ``OwnershipPath`` 传入，仍走同一套范围判定，
    不为创建路径开特例。
    """

    resource_type: str
    resource_id: str | None = None


@dataclass(frozen=True)
class OwnershipPath:
    """资源在归属链上的位置（Tenant → Organization → Project）。"""

    tenant_id: UUID
    organization_id: UUID | None = None
    project_id: str | None = None


class OwnershipResolver(Protocol):
    """归属解析端口（异步：需要一次数据库读）。"""

    async def resolve(
        self, resource_type: str, resource_id: str
    ) -> OwnershipPath | None: ...


# --------------------------------------------------------------------------- #
# 解析实现：每种资源一条显式 SQL（不拼接、不猜表）
# --------------------------------------------------------------------------- #
# 归属链只有一条路径：子资源 JOIN 父级，组织始终从 project 取。
# 资产/质检报告跨 schema 读 project_planning.projects 的三列归属键
# （迁移 b3c4d5e6f7a8 的列级授权），读不到项目业务字段。
_RESOLVE_SQL: dict[str, str] = {
    # 链的根：组织本身也要能解析，否则「创建时指定组织归属」这条路径无法用
    # 数据库事实回答「该组织是否在我这个租户内」，只能拿 principal 的租户拼一条
    # 必然自洽的路径（自证），把跨租户判定漏给外键、再被映射成 409。RLS 使跨租户
    # 查不到 → None → 统一 404，与其余资源同形。
    # 归属链根节点没有上级，project_id 恒为 NULL。
    "Organization": """
        SELECT o.tenant_id, o.organization_id, CAST(NULL AS text) AS project_id
        FROM project_planning.organizations AS o
        WHERE o.organization_id = CAST(:rid AS uuid)
    """,
    "Project": """
        SELECT p.tenant_id, p.organization_id, p.entity_id AS project_id
        FROM project_planning.projects AS p
        WHERE p.entity_id = :rid
    """,
    "Episode": """
        SELECT p.tenant_id, p.organization_id, p.entity_id AS project_id
        FROM project_planning.episodes AS e
        JOIN project_planning.projects AS p
          ON p.entity_id = e.project_id AND p.tenant_id = e.tenant_id
        WHERE e.entity_id = :rid
    """,
    "ScriptRevision": """
        SELECT p.tenant_id, p.organization_id, p.entity_id AS project_id
        FROM project_planning.script_revisions AS s
        JOIN project_planning.episodes AS e
          ON e.project_id = s.project_id AND e.episode_ref = s.episode_ref
        JOIN project_planning.projects AS p
          ON p.entity_id = e.project_id AND p.tenant_id = e.tenant_id
        WHERE s.entity_id = :rid
    """,
    # 场次的归属经 project_id 收敛到项目，不经 episode 两跳：scenes 自带
    # project_id，且三列外键保证它与集同项目（fk_scenes_episode_project）。
    # 多绕一跳会多一个「集被改挂」的窗口，而归属不会被改写（I-2）由守卫保证。
    "Scene": """
        SELECT p.tenant_id, p.organization_id, p.entity_id AS project_id
        FROM project_planning.scenes AS s
        JOIN project_planning.projects AS p
          ON p.entity_id = s.project_id AND p.tenant_id = s.tenant_id
        WHERE s.entity_id = :rid
    """,
    # Shot 的归属经 scene_id/project_id 收敛到项目；采用 shots 自带的三列归属键，
    # 与 (project_id, scene_id) 复合外键一致，不再绕两层父表制造可漂移的第二路径。
    "Shot": """
        SELECT p.tenant_id, p.organization_id, p.entity_id AS project_id
        FROM shot_planning.shots AS s
        JOIN project_planning.projects AS p
          ON p.entity_id = s.project_id AND p.tenant_id = s.tenant_id
        WHERE s.entity_id = :rid
    """,
    "AssetVersion": """
        SELECT p.tenant_id, p.organization_id, p.entity_id AS project_id
        FROM asset_service.asset_versions AS a
        JOIN project_planning.projects AS p
          ON p.entity_id = a.project_id AND p.tenant_id = a.tenant_id
        WHERE a.entity_id = :rid
    """,
    "QCReport": """
        SELECT p.tenant_id, p.organization_id, p.entity_id AS project_id
        FROM asset_service.qc_reports AS q
        JOIN asset_service.asset_versions AS a
          ON a.entity_id = q.asset_version_id
        JOIN project_planning.projects AS p
          ON p.entity_id = a.project_id AND p.tenant_id = a.tenant_id
        WHERE q.qc_report_id = :rid
    """,
}


class SqlOwnershipResolver:
    """归属解析的数据库实现（在调用方的事务/RLS 上下文内执行）。

    依赖 RLS：跨租户资源查不到 → 返回 None，由调用方统一转 404。这样「不存在」
    与「无权看见」在行为上天然同形，不需要额外判断。
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def resolve(
        self, resource_type: str, resource_id: str
    ) -> OwnershipPath | None:
        sql = _RESOLVE_SQL.get(resource_type)
        if sql is None:
            # 契约新增资源类型而这里没登记，属实现缺口，必须显式失败而不是放行。
            raise DomainError(
                ErrorRegistry.INTERNAL_ERROR,
                detail=f"资源类型 {resource_type} 未登记归属解析（禁止默认放行）",
            )
        row = (
            await self._session.execute(text(sql), {"rid": resource_id})
        ).first()
        if row is None:
            return None
        return OwnershipPath(
            tenant_id=row.tenant_id,
            organization_id=row.organization_id,
            project_id=row.project_id,
        )


# --------------------------------------------------------------------------- #
# 范围判定（授权入口调用）
# --------------------------------------------------------------------------- #
def assert_ownership_scope(
    principal: Principal,
    resource: ResourceRef,
    ownership: OwnershipPath | None,
) -> None:
    """判定主体能否作用于该资源（fail-closed）。

    判定顺序与语义见模块 docstring：不可见 → 404；同租户跨组织 → 403；
    组织项目对无 org claim 的主体一律拒绝（个人主体 / 服务账号不能读组织资产）。
    组织分支在现有调用路径上恒不触发（解析器受策略约束，只能返回同组织行），
    它守的是「将来有人绕过解析器直接拼归属路径」——判定来源仍是数据库策略。
    """
    if ownership is None:
        raise DomainError(
            ErrorRegistry.NOT_FOUND,
            detail=f"资源不存在或不可见: {resource.resource_type}/{resource.resource_id}",
        )

    # 纵深防御：解析语句已经带 JOIN 条件，但租户不一致意味着解析结果不可信
    # （例如未来有人改 SQL 漏了条件），此处再拦一次，且与「不可见」同形。
    if ownership.tenant_id != principal.tenant_id:
        raise DomainError(
            ErrorRegistry.NOT_FOUND,
            detail=f"资源不存在或不可见: {resource.resource_type}/{resource.resource_id}",
        )

    if ownership.organization_id is None:
        # 个人租户资源：无组织边界，租户内即达。
        return

    if principal.organization_id != ownership.organization_id:
        raise DomainError(
            ErrorRegistry.AUTH_SCOPE_DENIED,
            detail=(
                "资源归属其他组织：租户一致但组织不同属于越权访问"
                "（基线 27.3 多层级作用域）。如需访问，须由组织管理员授予该组织身份。"
            ),
            context={
                "resource_type": resource.resource_type,
                "resource_id": resource.resource_id,
                "resource_organization_id": str(ownership.organization_id),
                "principal_organization_id": (
                    str(principal.organization_id)
                    if principal.organization_id
                    else None
                ),
            },
        )
