"""ProductionRoute 应用服务（PRODUCTION.ROUTE.SELECT 选路用例）。

职责：
- ``select_route``（PRODUCTION.ROUTE.SELECT，MEDIUM，制片/TD/授权调度器）：
  为某镜头选定生产路线，并写下该镜头的**生效路线**（ShotRouteSelection）。
  前置条件（动作矩阵）：
  1. 路线存在（NOT_FOUND）；
  2. 路线处于 VERIFIED（ACT-ROUTE-NEG-001：UNVERIFIED 路线拒绝）；
  3. 满足 rights/capability —— capability 由授权层强制（token_required +
     require_action + 项目范围 ResourceRef）；rights 的可校验载体
     （RightsManifest/RightsCase，ADR-R24 债务）尚未建模，消费闸在交付域
     D3「消费前权利校验」承担——这里不伪造 rights 判定，诚实留待债务收口。
- 选中发出契约登记的审计事件 PRODUCTION_ROUTE_SELECTED（**只在真正登记
  生效路线时发一次**：幂等重放不制造第二条事实）。

「单一路线生成」（Phase 1 验收 C4，V205-R14-A9）的语义全部落在本用例：

| 情形 | 结果 |
| --- | --- |
| 该镜头尚无生效路线 | 登记路线并返回（created=True） |
| 已有生效路线，且与本次选择的路线**相同** | 幂等返回既有记录（created=False），不发事件 |
| 已有生效路线，但**不同** | 409 ``ROUTE_ALREADY_SELECTED``，details 带既有路线与选择 id |
| 两个请求并发、路线不同 | 数据库唯一约束裁决：一个成功，另一个走上面第三条 |

为什么"不同路线"直接拒绝而不是自动改路：改路是**治理动作**——它会让既有资产
按 ADR-028 产生 STALE 传播、需要审批、需要决定历史选择记录的处置。动作矩阵
没有任何 action 表达它，强行在本用例里自动改路等于用"选路"做了"改路"的事，
把一条需要审批的变更伪装成一次普通选择。

身份三要素（主体 / 租户 / 组织）只从 Principal 取（可信来源）。
选中不改变路线状态（UNVERIFIED→VERIFIED 的标定入口是适配器验证工作流，
契约未给 action）：选择是「使用事实」，路线状态是「能力事实」。
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.errors.repository_errors import DuplicateEntityError
from packages.identity.principal import Principal
from packages.shared.outbox_writer import OutboxWriter
from services.generation_execution.domain.enums import ProductionRouteStatus
from services.generation_execution.domain.ports import ShotOwnershipPort
from services.generation_execution.domain.production_route import ProductionRoute
from services.generation_execution.domain.shot_route_selection import (
    ShotRouteSelection,
)
from services.generation_execution.repository.production_route_repo import (
    ProductionRouteRepository,
)
from services.generation_execution.repository.shot_route_selection_repo import (
    ShotRouteSelectionRepository,
)


@dataclass(frozen=True)
class RouteSelectionResult:
    """选路结果（区分"新登记"与"幂等重放"）。

    ``created=False`` 表示该镜头早已在这条路线上：调用方拿到的是**既有的**
    生效路线记录，不是本次请求新建的。响应把它们区分开，调用方才能判断
    "我这次真的改变了什么"。
    """

    route: ProductionRoute
    selection: ShotRouteSelection
    created: bool


class RouteService:
    """生产路线选路用例。"""

    def __init__(
        self,
        route_repository: ProductionRouteRepository,
        selection_repository: ShotRouteSelectionRepository,
        outbox_writer: OutboxWriter,
        shot_ownership_port: ShotOwnershipPort,
    ) -> None:
        self._route_repository = route_repository
        self._selection_repository = selection_repository
        self._outbox_writer = outbox_writer
        self._shot_ownership_port = shot_ownership_port

    async def select_route(
        self,
        *,
        route_id: str,
        project_id: str,
        shot_id: str,
        principal: Principal,
    ) -> RouteSelectionResult:
        """为镜头选定生产路线（PRODUCTION.ROUTE.SELECT）。

        校验顺序刻意为先存在、后状态：幽灵路线 404、未验证路线 422，
        错误语义与错误码注册表一致（NOT_FOUND→404 / VALIDATION_ERROR→422）。

        镜头也必须是**真实存在且归属该项目**的：否则可以为幽灵镜头登记生效
        路线，唯一键被一个从未存在的对象占用，后续"该镜头已选路"的判断全部
        建立在虚空上。
        """
        route_id = route_id.strip()
        project_id = project_id.strip()
        shot_id = shot_id.strip()
        if not route_id:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR, detail="route_id 不能为空"
            )
        if not project_id:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR, detail="project_id 不能为空"
            )
        if not shot_id:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR, detail="shot_id 不能为空"
            )

        shot = await self._shot_ownership_port.resolve(
            tenant_id=principal.tenant_id,
            organization_id=principal.organization_id,
            shot_id=shot_id,
        )
        if shot is None:
            raise DomainError(
                ErrorRegistry.NOT_FOUND,
                detail=f"镜头不存在: {shot_id}（选路不得落在未确认的镜头上）",
            )
        if shot.project_id != project_id:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail=(
                    f"镜头 {shot_id} 归属项目 {shot.project_id}，"
                    f"与请求项目 {project_id} 不一致"
                ),
                context={"shot_id": shot_id},
            )

        route = await self._route_repository.get_by_id(route_id)
        if route is None:
            raise DomainError(
                ErrorRegistry.NOT_FOUND, detail=f"路线不存在: {route_id}"
            )
        if route.status is not ProductionRouteStatus.VERIFIED:
            # ACT-ROUTE-NEG-001：UNVERIFIED 路线拒绝（16.1 未验证能力不得
            # 作为生产路由的可用能力）。
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail=(
                    f"路线 {route_id} 状态为 {route.status.value}，"
                    "未验证路线不得被选中（须处于 VERIFIED）"
                ),
            )

        existing = await self._selection_repository.get_effective(
            project_id=project_id, shot_id=shot_id
        )
        if existing is not None:
            return self._resolve_against_effective(existing, route)

        selection = ShotRouteSelection(
            entity_id=f"srs_{uuid4().hex[:12]}",
            project_id=project_id,
            shot_id=shot_id,
            route_id=route.entity_id,
            created_by=principal.subject_id,
            tenant_id=principal.tenant_id,
            organization_id=principal.organization_id,
        )
        try:
            await self._selection_repository.add(selection)
        except DuplicateEntityError:
            # 并发裁决：另一个请求在同一瞬间为该镜头登记了生效路线。
            # 唯一约束是裁判，这里只负责把"输掉竞争"翻译成结构化结论。
            concurrent = await self._selection_repository.get_effective(
                project_id=project_id, shot_id=shot_id
            )
            if concurrent is None:
                # 唯一冲突却回查不到：约束与查询口径不一致（实现缺陷）。
                # 不能当成成功，否则"第二条路线被拒"会以静默通过的形式出现。
                raise DomainError(
                    ErrorRegistry.INTERNAL_ERROR,
                    detail=(
                        "生效路线登记冲突后无法回查既有记录"
                        f"（project_id={project_id}, shot_id={shot_id}）"
                        "——唯一约束与查询口径不一致，需排查"
                    ),
                ) from None
            return self._resolve_against_effective(concurrent, route)

        self._outbox_writer.record(
            # 事件聚合体仍是 ProductionRoute：动作矩阵把 PRODUCTION.ROUTE.SELECT
            # 的资源类型定为 ProductionRoute，审计事件必须与契约同口径；
            # 生效路线 id 作为载荷字段随事件记录（新增事实，不改聚合归属）。
            aggregate_type="ProductionRoute",
            aggregate_id=route.entity_id,
            event_type="PRODUCTION_ROUTE_SELECTED",
            tenant_id=principal.tenant_id,
            payload={
                "selection_id": selection.entity_id,
                "route_id": route.entity_id,
                "route_type": route.route_type,
                "project_id": project_id,
                "shot_id": shot_id,
                "selected_by": principal.subject_id,
                "tenant_id": str(principal.tenant_id),
                "status": route.status.value,
            },
        )
        return RouteSelectionResult(route=route, selection=selection, created=True)

    @staticmethod
    def _resolve_against_effective(
        existing: ShotRouteSelection, route: ProductionRoute
    ) -> RouteSelectionResult:
        """已有生效路线时的裁决：同路线幂等，异路线拒绝（第二条路线）。"""
        if existing.route_id == route.entity_id:
            # 幂等：同一请求重放，返回既有事实，不制造第二条记录、第二条事件。
            return RouteSelectionResult(
                route=route, selection=existing, created=False
            )
        raise DomainError(
            ErrorRegistry.ROUTE_ALREADY_SELECTED,
            detail=(
                f"镜头 {existing.project_id}/{existing.shot_id} 的生效路线已是 "
                f"{existing.route_id}，拒绝第二条路线 {route.entity_id}"
                "（单一路线生成，C4；变更路线属独立治理动作，当前未启用）"
            ),
            context={
                "project_id": existing.project_id,
                "shot_id": existing.shot_id,
                "effective_route_id": existing.route_id,
                "effective_selection_id": existing.entity_id,
                "requested_route_id": route.entity_id,
            },
        )
