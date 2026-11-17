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

本模块还承载**证据解析**适配器（``AssetQCReportRefAdapter`` /
``ShotPlanningSpecEvidenceAdapter``）：它们与归属解析共用同一套约束
（跨域只读、所有者域身份、只读探针），差别只在输出的事实集。放在一起是为了
让"组合根里可以跨域读"这件事只有一个落点——每多一个这样的模块，
R4（数据库身份旁路）白名单就多一个装配点，而装配点的数量就是这层防线的强度。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import UUID

from packages.identity.principal import Principal, SubjectType
from packages.shared.database import DomainPools
from packages.shared.db_roles import DatabaseDomain
from packages.shared.rls import set_request_context
from services.asset_service.domain.ports import SpecEvidence
from services.asset_service.repository.sqlalchemy_manifest_repo import (
    SqlAlchemyDeliveryManifestRepository,
)
from services.asset_service.repository.sqlalchemy_qc_repo import (
    SqlAlchemyQCReportRepository,
)
from services.generation_execution.domain.ports import RefOwnership, ShotFacts
from services.qc_governance.domain.ports import QCReportFacts
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


#: 镜头绑定抽取器（按资源类型登记）。返回 ``None`` = 该类型不做镜头级绑定。
#: 为什么单独一张表而不是在解析器里顺手塞字段：绑定来源是**所有者域的字段**，
#: 新增一种引用类型时"它绑不绑镜头、绑哪个字段"必须在这里被显式回答一次，
#: 漏登记就是 None（不做镜头校验）而不是静默通过某个猜测。
_SHOT_BINDINGS: dict[str, Callable[[Any], frozenset[str] | None]] = {
    "ShotSpec": lambda spec: frozenset({spec.shot_id}) if spec.shot_id else frozenset(),
    "ShotVisualPlan": (
        lambda plan: frozenset({plan.shot_id}) if plan.shot_id else frozenset()
    ),
    "ConstraintPack": lambda pack: frozenset(pack.shot_ids) if pack.shot_ids else frozenset(),
}


def _shot_ids_for(resource_type: str, entity: Any) -> frozenset[str] | None:
    """取出该引用的镜头绑定集合（未登记类型返回 None）。"""
    extractor = _SHOT_BINDINGS.get(resource_type)
    if extractor is None:
        return None
    return extractor(entity)


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
        shot_ids = _shot_ids_for(resource_type, entity)
        if evaluator is None:
            # 契约没有为该类型写明生成准入状态 → 不做判定（不猜规则）；
            # 镜头绑定仍然要带上：准入与归属是两条独立事实。
            return RefOwnership(project_id=entity.project_id, shot_ids=shot_ids)
        eligible, reason = evaluator(entity)
        return RefOwnership(
            project_id=entity.project_id,
            eligible=eligible,
            ineligible_reason="" if eligible else reason,
            shot_ids=shot_ids,
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


class AssetQCReportRefAdapter:
    """QC 报告引用解析适配器（qc_governance 端口的生产实现）。

    为什么不把 QC 报告塞进 ``_RESOLVERS`` 复用通用解析器：那里的契约是
    "只回答 project_id"（workflow / generation 两个消费域都不需要更多）。
    QC 复查还要求报告与**同一个资产版本**绑定——把这条塞进通用解析器会让
    两个无关消费域凭空多出一个它们不用的维度；单独一个适配器只多 20 行，
    却让每个端口的字段集都恰好是它需要的。

    读始终经过所有者：用 ``svc_asset`` 身份查 ``qc_reports``，qc_governance
    不获得该表的任何权限（ADR-R28 同一纪律）。
    """

    def __init__(self, pools: DomainPools) -> None:
        self._pools = pools

    async def resolve(
        self,
        *,
        tenant_id: object,
        organization_id: object,
        qc_report_id: str,
    ) -> QCReportFacts | None:
        sessionmaker = self._pools.sessionmaker(DatabaseDomain.ASSET)
        async with sessionmaker() as session:
            # 只读探针身份：项目范围取 * 以便区分"不存在"与"在别的项目"。
            await set_request_context(
                session,
                Principal(
                    subject_id="qc-report-resolver",
                    tenant_id=UUID(str(tenant_id)),
                    organization_id=UUID(str(organization_id)),
                    subject_type=SubjectType.AGENT,
                    project_ids=frozenset({"*"}),
                ),
            )
            report = await SqlAlchemyQCReportRepository(session).get_by_id(qc_report_id)
        if report is None:
            return None
        return QCReportFacts(
            qc_report_id=report.qc_report_id,
            project_id=report.project_id,
            asset_version_id=report.asset_version_id,
        )


class ShotPlanningShotOwnershipAdapter:
    """镜头归属解析适配器（generation 域 ``ShotOwnershipPort`` 的生产实现）。

    镜头的事实来源是 ``shot_revisions``（正式镜头内容，append-only）：有修订
    行即"该镜头存在"，行的 ``project_id`` 即归属。刻意不用 shot_drafts——
    草案是 AI 拆镜的中间产物，可以存在但没有进入正式镜头表，拿草案证明
    "镜头存在"会让选路落在一个从未被确认的镜头上。

    读经过所有者：用 ``svc_shot_planning`` 身份查询，generation 域不获得该表
    的任何权限（ADR-R28）。
    """

    def __init__(self, pools: DomainPools) -> None:
        self._pools = pools

    async def resolve(
        self,
        *,
        tenant_id: object,
        organization_id: object,
        shot_id: str,
    ) -> ShotFacts | None:
        sessionmaker = self._pools.sessionmaker(DatabaseDomain.SHOT_PLANNING)
        async with sessionmaker() as session:
            # 只读探针身份：项目范围取 * 以便区分"不存在"与"在别的项目"。
            await set_request_context(
                session,
                Principal(
                    subject_id="shot-ownership-resolver",
                    tenant_id=UUID(str(tenant_id)),
                    organization_id=UUID(str(organization_id)),
                    subject_type=SubjectType.AGENT,
                    project_ids=frozenset({"*"}),
                ),
            )
            revision = await SqlAlchemyShotRevisionRepository(
                session
            ).get_active_by_shot(shot_id)
        if revision is None:
            return None
        return ShotFacts(shot_id=revision.shot_id, project_id=revision.project_id)


class ShotPlanningSpecEvidenceAdapter:
    """ShotSpec 证据解析（资产域 ``SpecEvidencePort`` 的生产实现）。

    与上面几个适配器的关系：它们回答"这个对象归谁"（授权判定），本适配器回答
    "这条证据还成不成立"（交付清单重建，G1）。刻意放在**同一个组合根模块**而不是
    新建 `apps/api/evidence_ports.py`：新文件会因为 import `DomainPools` 而需要
    给 R4 白名单加第 9 个装配点，而装配点数量本身就是这层防线的强度
    （每加一个就多一处"可以挑权限更大的角色连接"的地方）。职责差异已由类名与
    端口类型表达，不需要再拆文件。
    """

    def __init__(self, pools: DomainPools) -> None:
        self._pools = pools

    async def resolve(
        self,
        *,
        tenant_id: UUID,
        organization_id: UUID | None,
        spec_id: str,
    ) -> SpecEvidence | None:
        """按 id 取规格事实；不可见/不存在返回 None（不区分两者，见端口文档）。"""
        sessionmaker = self._pools.sessionmaker(DatabaseDomain.SHOT_PLANNING)
        async with sessionmaker() as session:
            # 只读探针：项目范围取 * 才能区分"不存在"（MISSING）与"属于别的
            # 项目"（MISMATCHED）——两种结论对交付取证的意义完全不同。
            await set_request_context(
                session,
                Principal(
                    subject_id="spec-evidence-resolver",
                    tenant_id=tenant_id,
                    organization_id=organization_id or tenant_id,
                    subject_type=SubjectType.AGENT,
                    project_ids=frozenset({"*"}),
                ),
            )
            spec = await SqlAlchemyShotSpecRepository(session).get_by_id(spec_id)
        if spec is None:
            return None
        return SpecEvidence(
            spec_id=spec.entity_id,
            project_id=spec.project_id,
            shot_id=spec.shot_id,
            status=getattr(spec.status, "value", str(spec.status)),
            entity_version=spec.entity_version,
        )
