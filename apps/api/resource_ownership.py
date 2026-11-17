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

from typing import Any
from uuid import UUID

from packages.identity.principal import Principal, SubjectType
from packages.shared.database import DomainPools
from packages.shared.db_roles import DatabaseDomain
from packages.shared.rls import set_request_context
from services.asset_service.repository.sqlalchemy_manifest_repo import (
    SqlAlchemyDeliveryManifestRepository,
)
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


class CompositeResourceOwnershipPort:
    """按资源类型路由到所有者域只读仓储的归属解析适配器。"""

    def __init__(self, pools: DomainPools) -> None:
        self._pools = pools

    def can_resolve(self, resource_type: str) -> bool:
        return resource_type in _RESOLVERS

    async def resolve(
        self,
        *,
        tenant_id: object,
        organization_id: object,
        resource_type: str,
        resource_id: str,
    ) -> ResourceOwnership | None:
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
            entity = await repo_cls(session).get_by_id(resource_id)
            if entity is None:
                return None
            return ResourceOwnership(project_id=entity.project_id)
