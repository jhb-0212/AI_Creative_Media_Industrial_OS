"""资产域应用服务（用例编排层）。

职责：
- ASSET.VERSION.CREATE：新增不可变资产版本（GENERATED）。
- QC.REPORT.CREATE：登记检测证据；首个报告把资产从 GENERATED 推入 TECHNICAL_QC。
- ASSET.VERSION.APPROVE：授权主体批准（TECHNICAL_QC/HUMAN_REVIEW → APPROVED），
  强制职责分离、权利证据与 QC 证据齐备。

红线（每条都有对应测试）：
- 不覆盖旧版本：同一 asset_version_id 只能对应同一内容，否则拒绝。
- 不得自批：创建人（created_by）不能是批准人（approver），运行时真实主体比对。
- 缺证据不得通过：没有 QC 报告、没有 rights_manifest_ref 一律拒绝。
- 未登记状态转移一律拒绝（31.5），不猜测旁支状态（ADR-R08）。
- 审批与通用状态推进**共用一张边表**：边的所有者决定谁可推进，边的守卫决定
  推进需要什么证据。任何一条路径都不持有第二套规则（第二轮审查 P1-02）。

一致性与失败恢复：
- 状态变更与 Outbox 事件在同一工作单元内登记，由路由层统一 commit；
  任一步抛错则整体回滚，不留下「状态变了但没事件」或反之的半成品。
- 每次状态迁移都走乐观锁（expected_version），并发竞争转可重试错误。
"""

from __future__ import annotations

import uuid
from typing import Any

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.identity.principal import Principal
from packages.shared.outbox_writer import OutboxWriter
from packages.shared.port_errors import DuplicateEntityError, ReferentialIntegrityError
from packages.shared.versioning import ConcurrentModificationError
from services.asset_service.domain.asset_version import AssetVersion
from services.asset_service.domain.enums import (
    AssetStatus,
    DeviationClass,
    IntentMatchStatus,
)
from services.asset_service.domain.guards import GuardContext, evaluate
from services.asset_service.domain.qc_report import QCReport
from services.asset_service.domain.state_machine import (
    APPROVE_ACTION,
    QC_REPORT_ACTION,
    STATUS_CHANGE_ACTION,
    Transition,
    edge_owned_by,
    next_edge_owned_by,
)
from services.asset_service.repository.asset_repo import AssetRepository
from services.asset_service.repository.qc_repo import QCReportRepository

# 允许登记 QC 报告的状态：报告是技术检测证据，GENERATED/TECHNICAL_QC 都可追加。
_REPORTABLE_STATES = frozenset({AssetStatus.GENERATED, AssetStatus.TECHNICAL_QC})


def _assert_report_owner(report: QCReport, asset_version_id: str) -> None:
    """幂等键必须绑定同一资产：报告 id 跨资产复用即拒绝（不能"查到即返回"）。

    只校验"存在"会让 A 的质检结论被 B 静默继承——错误证据进入批准链，
    比报错危险得多。冲突走 409（IDEMPOTENCY_CONFLICT），不静默改写。
    """
    if report.asset_version_id != asset_version_id:
        raise DomainError(
            ErrorRegistry.IDEMPOTENCY_CONFLICT,
            detail=(
                f"qc_report_id {report.qc_report_id} 已属于资产版本 "
                f"{report.asset_version_id}，不得跨资产复用"
            ),
        )


def _same_content(
    existing: AssetVersion, sha256: str, asset_type: str, project_id: str
) -> bool:
    """判定既有资产与待登记内容是否一致（幂等判定的单一事实源）。

    同时比对 sha256、asset_type 与 project_id：
    - 同一文件被登记为不同 asset_type（如 video / audio）必须视为「内容不同」，
      否则并发回查路径会静默返回旧对象，而顺序路径会 409（复核 R5）；
    - project_id 不同意味着幂等键被另一个项目复用：若「查到即返回」，调用方
      会拿到**别的项目**的资产信封，等于跨项目信息泄漏 + 覆盖请求被默许。
    """
    return (
        existing.sha256 == sha256
        and existing.asset_type == asset_type
        and existing.project_id == project_id
    )


class AssetService:
    """资产版本创建、QC 证据登记、批准与查询用例。"""

    def __init__(
        self,
        assets: AssetRepository,
        qc_reports: QCReportRepository,
        outbox_writer: OutboxWriter,
    ) -> None:
        self._assets = assets
        self._qc_reports = qc_reports
        self._outbox_writer = outbox_writer

    # ------------------------------------------------------------------ #
    # ASSET.VERSION.CREATE
    # ------------------------------------------------------------------ #
    async def create_version(
        self,
        *,
        project_id: str,
        asset_type: str,
        sha256: str,
        storage_ref: dict[str, Any],
        source_asset_version_ids: list[str],
        rights_manifest_ref: str | None,
        adapter_id: str | None,
        tool_product: str | None,
        operator_ref: str | None,
        version: str | None,
        principal: Principal,
        entity_id: str | None = None,
    ) -> AssetVersion:
        """新增资产版本（幂等；同 id 不同内容视为覆盖请求，必须拒绝）。

        project_id 是归属键：资产的授权沿 project → organization 派生，
        没有归属的资产无法被授权判定（SCOPE-3）。归属真实性由数据库复合外键
        保证（跨租户挂靠被拒），组织范围由授权入口判定（AUTH-1 / S1）。
        """
        project_id = (project_id or "").strip()
        asset_type = asset_type.strip()
        normalized_sha = sha256.strip().lower()
        created_by = principal.subject_id.strip()

        new_entity_id = entity_id or uuid.uuid4().hex

        existing = await self._assets.get_by_id(new_entity_id)
        if existing is not None:
            # 同 id 同内容：重复提交，幂等返回既有版本。
            if _same_content(existing, normalized_sha, asset_type, project_id):
                return existing
            # 同 id 不同内容：这是覆盖旧版本，红线禁止（契约
            # ASSET.VERSION.CREATE「仅新增不可变版本，不覆盖旧版本」）。
            raise DomainError(
                ErrorRegistry.IDEMPOTENCY_CONFLICT,
                detail=(
                    f"asset_version_id {new_entity_id} 已存在且内容不同："
                    "禁止覆盖既有资产版本，请新增版本并引用 source_asset_version_ids"
                ),
            )

        asset = AssetVersion(
            entity_id=new_entity_id,
            created_by=created_by,
            tenant_id=principal.tenant_id,
            project_id=project_id,
            asset_type=asset_type,
            sha256=normalized_sha,
            storage_ref=storage_ref,
            source_asset_version_ids=list(source_asset_version_ids),
            rights_manifest_ref=_clean_optional(rights_manifest_ref),
            adapter_id=_clean_optional(adapter_id),
            tool_product=_clean_optional(tool_product),
            operator_ref=_clean_optional(operator_ref),
            version=_clean_optional(version),
        )

        try:
            await self._assets.add(asset)
        except ReferentialIntegrityError as exc:
            # project_id 归属外键不成立（项目在并发窗口里被删）或取值违反 CHECK：
            # 这不是「重复提交」，把它当幂等返回会掩盖归属已失效这个事实。
            raise DomainError(
                ErrorRegistry.CONCURRENT_MODIFICATION,
                detail="资产版本归属的项目已变化，请基于最新数据重试",
            ) from exc
        except DuplicateEntityError as exc:
            # 并发重复提交：主键冲突后回查。判定与快速路径共用同一纯函数，
            # 内容一致（sha256 且 asset_type）才幂等返回；不一致同样是「同 id
            # 不同内容」的覆盖请求，走 IDEMPOTENCY_CONFLICT 而非并发错误。
            existing = await self._assets.get_by_id(new_entity_id)
            if existing is not None and _same_content(
                existing, normalized_sha, asset_type, project_id
            ):
                return existing
            raise DomainError(
                ErrorRegistry.IDEMPOTENCY_CONFLICT,
                detail=(
                    f"asset_version_id {new_entity_id} 已存在且内容不同："
                    "禁止覆盖既有资产版本，请新增版本并引用 source_asset_version_ids"
                ),
            ) from exc

        self._outbox_writer.record(
            aggregate_type="AssetVersion",
            aggregate_id=asset.entity_id,
            tenant_id=principal.tenant_id,
            event_type="ASSET_VERSION_CREATED",
            payload={
                "asset_version_id": asset.entity_id,
                "project_id": asset.project_id,
                "asset_type": asset.asset_type,
                "sha256": asset.sha256,
                "storage_ref": asset.storage_ref,
                "source_asset_version_ids": asset.source_asset_version_ids,
                "rights_manifest_ref": asset.rights_manifest_ref,
                "created_by": created_by,
                "tenant_id": str(principal.tenant_id),
                "status": AssetStatus.GENERATED.value,
            },
        )
        return asset

    # ------------------------------------------------------------------ #
    # QC.REPORT.CREATE
    # ------------------------------------------------------------------ #
    async def create_qc_report(
        self,
        *,
        asset_version_id: str,
        quality_profile_version: str,
        detector_versions: dict[str, Any],
        findings: list[Any],
        principal: Principal,
        benchmark_version: str | None = None,
        deviation_class: DeviationClass | None = None,
        deviation_intent_ref: str | None = None,
        intent_match_status: IntentMatchStatus | None = None,
        human_decision: str | None = None,
        qc_report_id: str | None = None,
    ) -> tuple[AssetVersion, QCReport]:
        """登记 QC 证据；若资产仍在 GENERATED，首个报告将其推入 TECHNICAL_QC。"""
        asset = await self._assets.get_by_id(asset_version_id)
        if asset is None:
            raise DomainError(
                ErrorRegistry.NOT_FOUND, detail=f"资产版本不存在: {asset_version_id}"
            )
        if asset.status not in _REPORTABLE_STATES:
            raise DomainError(
                ErrorRegistry.STATE_TRANSITION_DENIED,
                detail=f"状态 {asset.status.value} 不接受新的 QC 报告",
            )

        report_id = qc_report_id or uuid.uuid4().hex
        existing = await self._qc_reports.get_by_id(report_id)
        if existing is not None:
            _assert_report_owner(existing, asset.entity_id)
            return asset, existing

        previous_status = asset.status
        report = QCReport(
            qc_report_id=report_id,
            asset_version_id=asset.entity_id,
            tenant_id=principal.tenant_id,
            quality_profile_version=quality_profile_version.strip(),
            detector_versions=detector_versions,
            findings=findings,
            created_by=principal.subject_id.strip(),
            benchmark_version=_clean_optional(benchmark_version),
            deviation_class=deviation_class,
            deviation_intent_ref=_clean_optional(deviation_intent_ref),
            intent_match_status=intent_match_status,
            human_decision=_clean_optional(human_decision),
        )

        try:
            await self._qc_reports.add(report)
        except ReferentialIntegrityError as exc:
            # 报告引用的资产版本不成立（并发删除）或取值违反 CHECK。
            raise DomainError(
                ErrorRegistry.CONCURRENT_MODIFICATION,
                detail="QC 报告引用的资产版本已变化，请重试",
            ) from exc
        except DuplicateEntityError as exc:
            existing = await self._qc_reports.get_by_id(report_id)
            if existing is not None:
                _assert_report_owner(existing, asset.entity_id)
                return asset, existing
            raise DomainError(
                ErrorRegistry.CONCURRENT_MODIFICATION,
                detail="QC 报告并发写入冲突，请重试",
            ) from exc

        if asset.status is AssetStatus.GENERATED:
            # 同一张边表：这条边属于 QC.REPORT.CREATE，且以「报告已登记」为准入
            # （因此该边无附加守卫——报告本身即证据，否则规则会自锁）。
            edge = edge_owned_by(
                AssetStatus.GENERATED, AssetStatus.TECHNICAL_QC, QC_REPORT_ACTION
            )
            evaluate(edge.guards, asset, GuardContext(actor=report.created_by))
            expected_version = asset.entity_version
            asset.transition_to(
                edge.target, audit_ref=f"qc_report:{report.qc_report_id}"
            )
            await self._assets.save(asset, expected_version)

        self._outbox_writer.record(
            aggregate_type="AssetVersion",
            aggregate_id=asset.entity_id,
            tenant_id=principal.tenant_id,
            event_type="QC_REPORT_CREATED",
            payload={
                "qc_report_id": report.qc_report_id,
                "asset_version_id": asset.entity_id,
                "quality_profile_version": report.quality_profile_version,
                "from_status": previous_status.value,
                "to_status": asset.status.value,
                "created_by": report.created_by,
                "tenant_id": str(principal.tenant_id),
            },
        )
        return asset, report

    # ------------------------------------------------------------------ #
    # ASSET.VERSION.APPROVE
    # ------------------------------------------------------------------ #
    async def approve(
        self,
        *,
        asset_version_id: str,
        principal: Principal,
    ) -> AssetVersion:
        """批准资产版本：沿审批链逐跳推进，守卫由边提供（不再各写一份）。

        契约主链 TECHNICAL_QC → HUMAN_REVIEW → APPROVED 的两条边都属于
        APPROVE 动作，因此这里用 ``next_edge_owned_by`` 逐跳走，每一跳都：
        求值该边的守卫 → 乐观锁保存 → 写一条审计事件。
        证据规则集中在 domain/guards.py，本方法不判断「缺什么证据」。
        """
        approved_by = principal.subject_id.strip()
        if not approved_by:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR, detail="approved_by 不能为空"
            )

        asset = await self._assets.get_by_id(asset_version_id)
        if asset is None:
            raise DomainError(
                ErrorRegistry.NOT_FOUND, detail=f"资产版本不存在: {asset_version_id}"
            )

        first_edge = next_edge_owned_by(asset.status, APPROVE_ACTION)
        if first_edge is None:
            raise DomainError(
                ErrorRegistry.STATE_TRANSITION_DENIED,
                detail=f"状态 {asset.status.value} 不可批准（须先完成技术 QC）",
            )

        context = await self._guard_context(
            asset, (first_edge, next_edge_owned_by(first_edge.target, APPROVE_ACTION)), approved_by
        )

        while asset.status is not AssetStatus.APPROVED:
            edge = next_edge_owned_by(asset.status, APPROVE_ACTION)
            if edge is None:  # 审批链走完仍非 APPROVED：契约与实现不一致
                raise DomainError(
                    ErrorRegistry.STATE_TRANSITION_DENIED,
                    detail=f"状态 {asset.status.value} 无可用的审批边",
                )
            # 事件名在调用点写死，不用变量拼接：事件契约闸门按字面量比对
            # emitted_by，变量会让「代码真的发了这个事件」变得不可静态验证。
            if edge.target is AssetStatus.HUMAN_REVIEW:
                await self._advance(
                    asset,
                    edge,
                    context,
                    principal=principal,
                    event_type="ASSET_VERSION_HUMAN_REVIEW_ENTERED",
                )
            else:
                await self._advance(
                    asset,
                    edge,
                    context,
                    principal=principal,
                    event_type="ASSET_VERSION_APPROVED",
                )
        return asset

    # ------------------------------------------------------------------ #
    # ASSET.STATUS.CHANGE
    # ------------------------------------------------------------------ #
    async def change_status(
        self,
        *,
        asset_version_id: str,
        target_status: AssetStatus,
        changed_by: str,
        principal: Principal,
    ) -> AssetVersion:
        """推进资产状态（ASSET.STATUS.CHANGE）。

        与 approve 共用同一张边表：先取边，再校验这条边是否归本动作所有，
        最后求值这条边自带的守卫。因此：

        - ``APPROVED`` 不可由本接口到达——那条边的所有者是 ASSET.VERSION.APPROVE，
          通用接口在结构上不可达，不依赖额外的 if；
        - ``GOLD`` 的权利/QC/职责分离门槛与审批同一份实现，不存在两套规则；
        - 未登记转移一律拒绝（31.5）。
        """
        changed_by = changed_by.strip()
        if not changed_by:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR, detail="changed_by 不能为空"
            )

        asset = await self._assets.get_by_id(asset_version_id)
        if asset is None:
            raise DomainError(
                ErrorRegistry.NOT_FOUND, detail=f"资产版本不存在: {asset_version_id}"
            )

        edge = edge_owned_by(asset.status, target_status, STATUS_CHANGE_ACTION)
        context = await self._guard_context(asset, (edge,), changed_by)
        await self._advance(
            asset,
            edge,
            context,
            principal=principal,
            event_type="ASSET_VERSION_STATUS_CHANGED",
        )
        return asset

    # ------------------------------------------------------------------ #
    # 迁移执行（所有推进路径的唯一落库点）
    # ------------------------------------------------------------------ #
    async def _guard_context(
        self,
        asset: AssetVersion,
        edges: tuple[Transition | None, ...],
        actor: str,
    ) -> GuardContext:
        """按边的需要取齐外部事实（只有 QC 计数需要一次查库）。"""
        needs_qc = any(
            guard.needs_qc_count
            for edge in edges
            if edge is not None
            for guard in edge.guards
        )
        count = (
            await self._qc_reports.count_by_asset(asset.entity_id) if needs_qc else 0
        )
        return GuardContext(actor=actor, qc_report_count=count)

    async def _advance(
        self,
        asset: AssetVersion,
        edge: Transition,
        context: GuardContext,
        *,
        principal: Principal,
        event_type: str,
    ) -> None:
        """求值守卫 → 乐观锁迁移 → 同事务写事件（R07）。"""
        evaluate(edge.guards, asset, context)

        previous_status = asset.status
        expected_version = asset.entity_version
        asset.transition_to(edge.target, audit_ref=f"{edge.owner_action}:{context.actor}")
        try:
            await self._assets.save(asset, expected_version)
        except ConcurrentModificationError as exc:
            raise DomainError(
                ErrorRegistry.CONCURRENT_MODIFICATION,
                detail="资产状态并发冲突，请基于最新状态重试",
            ) from exc

        self._outbox_writer.record(
            aggregate_type="AssetVersion",
            aggregate_id=asset.entity_id,
            tenant_id=principal.tenant_id,
            event_type=event_type,
            payload={
                "asset_version_id": asset.entity_id,
                "project_id": asset.project_id,
                "from_status": previous_status.value,
                "to_status": edge.target.value,
                "actor": context.actor,
                "owner_action": edge.owner_action,
                "created_by": asset.created_by,
                "qc_report_count": context.qc_report_count,
                "tenant_id": str(principal.tenant_id),
            },
        )

    # ------------------------------------------------------------------ #
    # ASSET.VERSION.READ
    # ------------------------------------------------------------------ #
    async def get_version(self, *, asset_version_id: str) -> AssetVersion:
        """读取资产版本信封（只读，无状态变更、无事件）。

        可见性由 RLS 决定：租户外资产对当前会话不可见，因此「不存在」与
        「无权可见」统一返回 NOT_FOUND，避免通过 404/403 差异探测他人租户资产。
        """
        asset = await self._assets.get_by_id(asset_version_id)
        if asset is None:
            raise DomainError(
                ErrorRegistry.NOT_FOUND, detail=f"资产版本不存在: {asset_version_id}"
            )
        return asset


def _clean_optional(value: str | None) -> str | None:
    """可选字符串归一：去空白，空串转 None（避免 '' 与 NULL 两种「空」）。"""
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None
