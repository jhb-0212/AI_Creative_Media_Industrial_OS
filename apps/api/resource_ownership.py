"""跨域资源归属解析的组合根适配器（ResourceOwnershipPort 的生产实现）。

为什么放在 apps/api（组合根）而不是任何服务内：
- workflow 域只定义端口协议（``services.workflow.domain.ports``）；
- 适配器要 import asset_service / shot_planning 的**只读仓储**——这种
  跨服务 import 只允许出现在组合根（R2：领域模块之间禁止直连）；
- 适配器用**资源所有者域自己的**连接池（``svc_asset`` / ``svc_shot_planning``
  数据库身份）查询：读永远经过所有者，workflow 不获得任何他域表权限。

归属解析按租户全域注入 RLS 上下文（项目范围 ``*``）：只有看得见跨项目
行，才能区分「不存在」与「在别的项目」两种拒绝原因。适配器只输出
``ResourceOwnership``（project_id 一个字段），不向 workflow 泄漏业务数据。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import UUID

from packages.identity.principal import Principal, SubjectType
from packages.shared.database import DomainPools
from packages.shared.db_roles import DatabaseDomain
from packages.shared.rls import set_request_context
from services.asset_service.repository.sqlalchemy_manifest_repo import (
    SqlAlchemyDeliveryManifestRepository,
)
from services.generation_execution.domain.ports import RefOwnership
from services.shot_planning.domain.enums import ConstraintPackStatus
from services.shot_planning.repository.constraint_pack_sqlalchemy_repo import (
    SqlAlchemyConstraintPackRepository,
)
from services.shot_planning.repository.shot_draft_sqlalchemy_repo import (
    SqlAlchemyShotDraftRepository,
)
from services.shot_planning.repository.shot_revision_sqlalchemy_repo import (
    SqlAlchemyShotRevisionRepository,
)
from services.shot_planning.repository.shot_spec_sqlalchemy_repo import (
    SqlAlchemyShotSpecRepository,
)
from services.shot_planning.repository.shot_visual_plan_sqlalchemy_repo import (
    SqlAlchemyShotVisualPlanRepository,
)
from services.workflow.domain.ports import ResourceOwnership

#: 资源类型 → (所属域, 解析协程)。登记式路由：新增类型在这里加一行，
#: 没登记的类型由服务层 fail-closed 拒绝（不盲发）。
_RESOLVERS: dict[str, tuple[DatabaseDomain, Any]] = {
    "DeliveryManifest": (
        DatabaseDomain.ASSET,
        SqlAlchemyDeliveryManifestRepository,
    ),
    "ShotDraft": (
        DatabaseDomain.SHOT_PLANNING,
        SqlAlchemyShotDraftRepository,
    ),
    "ShotRevision": (
        DatabaseDomain.SHOT_PLANNING,
        SqlAlchemyShotRevisionRepository,
    ),
    "ShotSpec": (
        DatabaseDomain.SHOT_PLANNING,
        SqlAlchemyShotSpecRepository,
    ),
    "ShotVisualPlan": (
        DatabaseDomain.SHOT_PLANNING,
        SqlAlchemyShotVisualPlanRepository,
    ),
    "ConstraintPack": (
        DatabaseDomain.SHOT_PLANNING,
        SqlAlchemyConstraintPackRepository,
    ),
}

#: 生成准入判定（按资源类型登记）。**只登记契约明写的规则**：
#: 7.9「只有 ACTIVE 可作为生成输入」——未登记的类型不给判定，
#: 不把"看起来应该"的规则猜成契约（猜出来的规则比没有规则更危险：
#: 它会让一个错误的前提在测试里全绿）。
_ADMISSION_EVALUATORS: dict[str, Callable[[Any], tuple[bool, str]]] = {
    "ConstraintPack": lambda pack: (
        pack.status is ConstraintPackStatus.ACTIVE,
        f"约束包状态为 {pack.status.value}，只有 ACTIVE 可作为生成输入（7.9）",
    ),
}


class CompositeResourceOwnershipPort:
    """按资源类型路由到所有者域只读仓储的归属解析适配器。"""

    def __init__(self, pools: DomainPools) -> None:
        self._pools = pools

    def can_resolve(self, resource_type: str) -> bool:
        return resource_type in _RESOLVERS

    async def _load(
        self,
        *,
        tenant_id: object,
        organization_id: object,
        resource_type: str,
        resource_id: str,
    ) -> Any | None:
        """以所有者域身份加载对象（读永远经过所有者，见模块 docstring）。"""
        route = _RESOLVERS.get(resource_type)
        if route is None:
            return None
        domain, repo_cls = route
        sessionmaker = self._pools.sessionmaker(domain)
        async with sessionmaker() as session:
            # 解析身份：适配器自身的只读探针（不是任何请求主体），
            # 项目范围取 * 以便区分「不存在」与「在别的项目」。
            await set_request_context(
                session,
                Principal(
                    subject_id="ownership-resolver",
                    tenant_id=UUID(str(tenant_id)),
                    organization_id=UUID(str(organization_id)),
                    subject_type=SubjectType.AGENT,
                    project_ids=frozenset({"*"}),
                ),
            )
            return await repo_cls(session).get_by_id(resource_id)

    async def resolve(
        self,
        *,
        tenant_id: object,
        organization_id: object,
        resource_type: str,
        resource_id: str,
    ) -> ResourceOwnership | None:
        entity = await self._load(
            tenant_id=tenant_id,
            organization_id=organization_id,
            resource_type=resource_type,
            resource_id=resource_id,
        )
        if entity is None:
            return None
        return ResourceOwnership(project_id=entity.project_id)

    async def resolve_generation_input(
        self,
        *,
        tenant_id: object,
        organization_id: object,
        resource_type: str,
        resource_id: str,
    ) -> RefOwnership | None:
        """解析归属 + 所有者域对"能否作为生成输入"的判定（generation 端口形态）。

        与 ``resolve`` 分两个方法而不是给一个方法加开关：两个消费域的端口
        字段不同（workflow 只要归属，generation 还要准入结论），合并会让
        workflow 的端口凭空多出一个它不理解的字段。
        """
        entity = await self._load(
            tenant_id=tenant_id,
            organization_id=organization_id,
            resource_type=resource_type,
            resource_id=resource_id,
        )
        if entity is None:
            return None
        evaluator = _ADMISSION_EVALUATORS.get(resource_type)
        if evaluator is None:
            # 契约没有为该类型写明生成准入状态 → 不做判定（不猜规则）。
            return RefOwnership(project_id=entity.project_id)
        eligible, reason = evaluator(entity)
        return RefOwnership(
            project_id=entity.project_id,
            eligible=eligible,
            ineligible_reason="" if eligible else reason,
        )


class GenerationRefOwnershipAdapter:
    """组合根解析器 → generation 域 RefOwnershipPort 的显式转换适配器。

    为什么需要这一层而不是直接把 ``CompositeResourceOwnershipPort`` 塞给
    generation 服务：两个域的归属数据类（workflow 的 ``ResourceOwnership`` /
    generation 的 ``RefOwnership``）是各自领域的名义类型，字段**不同**
    （generation 多一个所有者域准入结论 ``eligible``）——靠 mypy 结构匹配
    会把两个域焊死，等于在领域之间开一条隐性类型通道。转换在这里
    （组合根）显式发生，依赖方向保持单向。
    """

    def __init__(self, composite: CompositeResourceOwnershipPort) -> None:
        self._composite = composite

    def can_resolve(self, resource_type: str) -> bool:
        return self._composite.can_resolve(resource_type)

    async def resolve(
        self,
        *,
        tenant_id: object,
        organization_id: object,
        resource_type: str,
        resource_id: str,
    ) -> RefOwnership | None:
        return await self._composite.resolve_generation_input(
            tenant_id=tenant_id,
            organization_id=organization_id,
            resource_type=resource_type,
            resource_id=resource_id,
        )
