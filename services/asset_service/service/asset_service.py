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

一致性与失败恢复：
- 状态变更与 Outbox 事件在同一工作单元内登记，由路由层统一 commit；
  任一步抛错则整体回滚，不留下「状态变了但没事件」或反之的半成品。
- 每次状态迁移都走乐观锁（expected_version），并发竞争转可重试错误。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.exc import IntegrityError

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.identity.principal import Principal
from packages.shared.outbox_writer import OutboxWriter
from services.asset_service.domain.asset_version import AssetVersion
from services.asset_service.domain.enums import (
    AssetStatus,
    DeviationClass,
    IntentMatchStatus,
)
from services.asset_service.domain.qc_report import QCReport
from services.asset_service.repository.asset_repo import AssetRepository
from services.asset_service.repository.qc_repo import QCReportRepository

# 允许批准动作发起的状态：TECHNICAL_QC 表示「技术 QC 已完成，进入人工复核」，
# HUMAN_REVIEW 表示已处于人工复核。两者都在契约主链上，未登记状态一律拒绝。
_APPROVABLE_STATES = frozenset({AssetStatus.TECHNICAL_QC, AssetStatus.HUMAN_REVIEW})
# 允许登记 QC 报告的状态：报告是技术检测证据，GENERATED/TECHNICAL_QC 都可追加。
_REPORTABLE_STATES = frozenset({AssetStatus.GENERATED, AssetStatus.TECHNICAL_QC})


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
        """新增资产版本（幂等；同 id 不同内容视为覆盖请求，必须拒绝）。"""
        asset_type = asset_type.strip()
        normalized_sha = sha256.strip().lower()
        created_by = principal.subject_id.strip()

        new_entity_id = entity_id or uuid.uuid4().hex

        existing = await self._assets.get_by_id(new_entity_id)
        if existing is not None:
            # 同 id 同内容：重复提交，幂等返回既有版本。
            if existing.sha256 == normalized_sha and existing.asset_type == asset_type:
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
        except IntegrityError as exc:
            # 并发重复提交：主键冲突后回查，内容一致则幂等返回。
            existing = await self._assets.get_by_id(new_entity_id)
            if existing is not None and existing.sha256 == normalized_sha:
                return existing
            raise DomainError(
                ErrorRegistry.CONCURRENT_MODIFICATION,
                detail="资产版本并发写入冲突，请查询后重试",
            ) from exc

        self._outbox_writer.record(
            aggregate_type="AssetVersion",
            aggregate_id=asset.entity_id,
            tenant_id=principal.tenant_id,
            event_type="ASSET_VERSION_CREATED",
            payload={
                "asset_version_id": asset.entity_id,
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
            intent_match_status=intent_match_status,
            human_decision=_clean_optional(human_decision),
        )

        try:
            await self._qc_reports.add(report)
        except IntegrityError as exc:
            existing = await self._qc_reports.get_by_id(report_id)
            if existing is not None:
                return asset, existing
            raise DomainError(
                ErrorRegistry.CONCURRENT_MODIFICATION,
                detail="QC 报告并发写入冲突，请重试",
            ) from exc

        if asset.status is AssetStatus.GENERATED:
            expected_version = asset.entity_version
            asset.transition_to(
                AssetStatus.TECHNICAL_QC,
                audit_ref=f"qc_report:{report.qc_report_id}",
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
        """批准资产版本（职责分离 + 权利证据 + QC 证据，缺一不可）。"""
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

        if asset.status not in _APPROVABLE_STATES:
            raise DomainError(
                ErrorRegistry.STATE_TRANSITION_DENIED,
                detail=f"状态 {asset.status.value} 不可批准（须先完成技术 QC）",
            )

        # 职责分离：创建人与批准人必须是不同主体（禁止自批，Agent 不能自审自批）。
        if asset.created_by == approved_by:
            raise DomainError(
                ErrorRegistry.STATE_TRANSITION_DENIED,
                detail="资产创建人与批准人不得为同一主体（职责分离红线）",
            )

        # 权利证据：没有 rights_manifest_ref 的资产不得进入 APPROVED。
        if not asset.rights_manifest_ref:
            raise DomainError(
                ErrorRegistry.STATE_TRANSITION_DENIED,
                detail="缺少 rights_manifest_ref：无权利证据不得批准",
            )

        # QC 证据：批准前必须至少有一份检测报告。
        report_count = await self._qc_reports.count_by_asset(asset.entity_id)
        if report_count == 0:
            raise DomainError(
                ErrorRegistry.STATE_TRANSITION_DENIED,
                detail="缺少 QC 报告：无检测证据不得批准",
            )

        # 主链完整性：TECHNICAL_QC 必须先经过 HUMAN_REVIEW 才到 APPROVED
        # （两次迁移分两次乐观锁保存，逐跳推进，不跳关）。
        previous_status = asset.status
        expected_version = asset.entity_version
        reviewed_now = False
        if asset.status is AssetStatus.TECHNICAL_QC:
            asset.transition_to(
                AssetStatus.HUMAN_REVIEW, audit_ref=f"human_review_by:{approved_by}"
            )
            await self._assets.save(asset, expected_version)
            expected_version = asset.entity_version
            reviewed_now = True

        asset.transition_to(
            AssetStatus.APPROVED, audit_ref=f"approved_by:{approved_by}"
        )
        await self._assets.save(asset, expected_version)

        self._outbox_writer.record(
            aggregate_type="AssetVersion",
            aggregate_id=asset.entity_id,
            tenant_id=principal.tenant_id,
            event_type="ASSET_VERSION_APPROVED",
            payload={
                "asset_version_id": asset.entity_id,
                "from_status": previous_status.value,
                "to_status": AssetStatus.APPROVED.value,
                "approved_by": approved_by,
                "created_by": asset.created_by,
                "qc_report_count": report_count,
                "human_review_in_same_call": reviewed_now,
                "tenant_id": str(principal.tenant_id),
            },
        )
        return asset


def _clean_optional(value: str | None) -> str | None:
    """可选字符串归一：去空白，空串转 None（避免 '' 与 NULL 两种「空」）。"""
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None
