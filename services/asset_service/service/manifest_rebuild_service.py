"""交付清单端到端重建（验收 C6/G1 的读取侧能力）。

要解决的问题：`DELIVERY.MANIFEST.CREATE` 只保证"创建时引用成立"，
而 G1 要求的是**事后**能从清单重建一次交付：逐项把引用取回来、逐项比对、
缺什么就明说缺什么。两者失败方式不同——创建时的校验漏掉一项，清单已经发出去了。

三条设计决定：

1. **重建结果与原件哈希一致，靠"用重建出来的证据算哈希"，不靠"读回原样的
   字符串"**。如果直接拿清单字段再算一遍校验和，那是恒等式，什么也没证明。
   这里先解析每个引用、只用解析成功的证据组装视图，再算校验和；只要有一项
   解析失败，校验和就会不同——"缺证据"因此表现为事实差异，而不是一句提示。
2. **缺项分三类，不合并**：
   - `MISSING`：引用的对象不存在（该报错）；
   - `MISMATCHED`：对象存在但内容/归属与清单不符（更危险，说明证据被替换过）；
   - `NOT_MODELED`：这类证据在 Phase 1 还没有对象模型，**无法校验**。
   把第三类混进"缺失"会让真实缺陷被结构性缺口掩盖；混进"通过"则是撒谎。
3. **不做字节级内容校验**（对 `storage_ref.uri` 取回文件重算 sha256）：
   平台尚无对象存储适配器（`adapters/storage/` 只有 `.gitkeep`）。这一层
   证不出来就写明证不出来（见 ADR-R33 §4），不用"元数据一致"冒充"内容一致"。

本服务只读：不推进任何状态、不写事件、不改清单。重建是取证动作，不是业务动作。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from packages.errors.error_codes import DomainError, ErrorRegistry
from services.asset_service.domain.delivery_manifest import (
    DeliveryManifest,
    compute_manifest_checksum,
)
from services.asset_service.domain.enums import AssetStatus
from services.asset_service.domain.ports import SpecEvidencePort
from services.asset_service.repository.asset_repo import AssetRepository
from services.asset_service.repository.manifest_repo import (
    DeliveryManifestRepository,
)
from services.asset_service.repository.qc_repo import QCReportRepository


class EvidenceRefType(StrEnum):
    """清单里可能出现的证据类型（与 DeliveryManifest 字段一一对应）。"""

    ASSET_VERSION = "asset_version"
    QC_REPORT = "qc_report"
    SHOT_SPEC = "shot_spec"
    PICTURE_LOCK = "picture_lock"
    RIGHTS_EVIDENCE = "rights_evidence"


class EvidenceState(StrEnum):
    """单条证据的重建结论（四态，互不重叠）。"""

    VERIFIED = "VERIFIED"
    MISSING = "MISSING"
    MISMATCHED = "MISMATCHED"
    NOT_MODELED = "NOT_MODELED"


#: 可作为交付内容的资产状态。与 `delivery_service._DELIVERABLE_STATES`
#: 必须一致——但刻意不 import 那个私有常量：两个用例（创建 / 重建）对
#: "可交付"的判断如果共享同一个私有名字，改一处就会静默影响另一处行为。
#: 一致性由集成用例保证（同一份清单在两条路径上得到同一结论）。
_DELIVERABLE_STATES = frozenset({AssetStatus.APPROVED, AssetStatus.GOLD})

_SHA256_HEX_LEN = 64


@dataclass(frozen=True)
class EvidenceItem:
    """一条证据的重建结果。"""

    ref_type: EvidenceRefType
    ref: str
    state: EvidenceState
    #: 解析成功时带上的可比对事实（不含业务内容，只含身份/状态/摘要）。
    facts: dict[str, Any] = field(default_factory=dict)
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ref_type": self.ref_type.value,
            "ref": self.ref,
            "state": self.state.value,
            "facts": self.facts,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class ManifestRebuildReport:
    """重建报告：原件与重建结果的逐项比对。"""

    manifest_id: str
    project_id: str
    original_checksum: str
    rebuilt_checksum: str
    items: tuple[EvidenceItem, ...]

    @property
    def checksum_match(self) -> bool:
        """重建校验和是否与原件一致（不一致 = 这次交付重建不出来）。"""
        return self.original_checksum == self.rebuilt_checksum

    @property
    def missing(self) -> tuple[EvidenceItem, ...]:
        """找不到或与清单不符的证据（真实缺陷，必须修）。"""
        return tuple(
            item
            for item in self.items
            if item.state in (EvidenceState.MISSING, EvidenceState.MISMATCHED)
        )

    @property
    def not_modeled(self) -> tuple[EvidenceItem, ...]:
        """Phase 1 尚无对象模型、因此无法校验的证据（结构性缺口）。"""
        return tuple(item for item in self.items if item.state is EvidenceState.NOT_MODELED)

    @property
    def verified(self) -> tuple[EvidenceItem, ...]:
        return tuple(item for item in self.items if item.state is EvidenceState.VERIFIED)

    @property
    def complete(self) -> bool:
        """完整重建：校验和一致且没有任何未校验证据。"""
        return self.checksum_match and not self.missing and not self.not_modeled

    def to_dict(self) -> dict[str, Any]:
        return {
            "manifest_id": self.manifest_id,
            "project_id": self.project_id,
            "original_checksum": self.original_checksum,
            "rebuilt_checksum": self.rebuilt_checksum,
            "checksum_match": self.checksum_match,
            "complete": self.complete,
            "counts": {
                "verified": len(self.verified),
                "missing": len(self.missing),
                "not_modeled": len(self.not_modeled),
            },
            "items": [item.to_dict() for item in self.items],
        }


class DeliveryManifestRebuildService:
    """按清单重建一次交付（只读取证）。"""

    def __init__(
        self,
        *,
        manifests: DeliveryManifestRepository,
        assets: AssetRepository,
        qc_reports: QCReportRepository,
        spec_evidence: SpecEvidencePort,
    ) -> None:
        self._manifests = manifests
        self._assets = assets
        self._qc_reports = qc_reports
        self._spec_evidence = spec_evidence

    async def rebuild(self, manifest_id: str) -> ManifestRebuildReport:
        """重建清单：逐项解析证据 → 只用已解析的证据重算校验和。"""
        manifest = await self._manifests.get_by_id(manifest_id)
        if manifest is None:
            raise DomainError(
                ErrorRegistry.NOT_FOUND, detail=f"交付清单不存在: {manifest_id}"
            )

        items: list[EvidenceItem] = []
        resolved_assets: list[str] = []
        for asset_ref in manifest.asset_refs:
            item = await self._resolve_asset(asset_ref, project_id=manifest.project_id)
            items.append(item)
            if item.state is EvidenceState.VERIFIED:
                resolved_assets.append(asset_ref)

        qc_item = await self._resolve_qc_report(
            manifest.qc_final_ref,
            project_id=manifest.project_id,
            asset_refs=manifest.asset_refs,
        )
        items.append(qc_item)

        spec_item = await self._resolve_spec(manifest.spec_ref, manifest=manifest)
        items.append(spec_item)

        items.extend(self._unmodeled_refs(manifest))

        rebuilt = compute_manifest_checksum(
            asset_refs=sorted(resolved_assets),
            # 未解析的 spec / qc 用空串参与计算：这样"缺证据"必然改变校验和，
            # 而不是让残缺的重建结果与原件恰好相等（那才是最难发现的假绿）。
            spec_ref=manifest.spec_ref if spec_item.state is EvidenceState.VERIFIED else "",
            qc_final_ref=(
                manifest.qc_final_ref if qc_item.state is EvidenceState.VERIFIED else ""
            ),
            picture_lock_ref=manifest.picture_lock_ref,
            rights_evidence_refs=manifest.rights_evidence_refs,
            rebuild_recipe=manifest.rebuild_recipe,
        )
        return ManifestRebuildReport(
            manifest_id=manifest.manifest_id,
            project_id=manifest.project_id,
            original_checksum=manifest.checksum,
            rebuilt_checksum=rebuilt,
            items=tuple(items),
        )

    async def _resolve_asset(self, asset_ref: str, *, project_id: str) -> EvidenceItem:
        """资产证据：存在 → 归属正确 → 状态可交付 → 摘要与地址自洽。"""
        asset = await self._assets.get_by_id(asset_ref)
        if asset is None:
            return EvidenceItem(
                EvidenceRefType.ASSET_VERSION,
                asset_ref,
                EvidenceState.MISSING,
                reason="引用的资产版本不存在（清单指向了已消失的对象）",
            )
        facts = {
            "project_id": asset.project_id,
            "status": asset.status.value,
            "sha256": asset.sha256,
            "storage_uri": str(asset.storage_ref.get("uri", "")),
            "entity_version": asset.entity_version,
        }
        if asset.project_id != project_id:
            return EvidenceItem(
                EvidenceRefType.ASSET_VERSION,
                asset_ref,
                EvidenceState.MISMATCHED,
                facts=facts,
                reason=f"资产归属项目 {asset.project_id}，与清单项目 {project_id} 不一致",
            )
        if asset.status not in _DELIVERABLE_STATES:
            return EvidenceItem(
                EvidenceRefType.ASSET_VERSION,
                asset_ref,
                EvidenceState.MISMATCHED,
                facts=facts,
                reason=(
                    f"资产状态为 {asset.status.value}：重建出的交付不应包含"
                    "未批准的版本（清单发出后状态被回退，也是证据漂移）"
                ),
            )
        if len(asset.sha256) != _SHA256_HEX_LEN:
            return EvidenceItem(
                EvidenceRefType.ASSET_VERSION,
                asset_ref,
                EvidenceState.MISMATCHED,
                facts=facts,
                reason=f"资产摘要长度 {len(asset.sha256)}，不是 sha256 十六进制串",
            )
        if not asset.storage_ref.get("uri"):
            return EvidenceItem(
                EvidenceRefType.ASSET_VERSION,
                asset_ref,
                EvidenceState.MISMATCHED,
                facts=facts,
                reason="资产缺少 storage_ref.uri：重建时取不回内容地址",
            )
        return EvidenceItem(
            EvidenceRefType.ASSET_VERSION, asset_ref, EvidenceState.VERIFIED, facts=facts
        )

    async def _resolve_qc_report(
        self, qc_ref: str, *, project_id: str, asset_refs: list[str]
    ) -> EvidenceItem:
        """QC 证据：存在 → 归属正确 → **必须针对本清单里的资产**。"""
        report = await self._qc_reports.get_by_id(qc_ref)
        if report is None:
            return EvidenceItem(
                EvidenceRefType.QC_REPORT,
                qc_ref,
                EvidenceState.MISSING,
                reason="引用的 QC 报告不存在（终检证据缺失）",
            )
        facts = {
            "asset_version_id": report.asset_version_id,
            "project_id": report.project_id,
            "quality_profile_version": report.quality_profile_version,
            "created_by": report.created_by,
        }
        if report.project_id != project_id:
            return EvidenceItem(
                EvidenceRefType.QC_REPORT,
                qc_ref,
                EvidenceState.MISMATCHED,
                facts=facts,
                reason=f"QC 报告归属项目 {report.project_id}，与清单项目 {project_id} 不一致",
            )
        if report.asset_version_id not in asset_refs:
            return EvidenceItem(
                EvidenceRefType.QC_REPORT,
                qc_ref,
                EvidenceState.MISMATCHED,
                facts=facts,
                reason=(
                    f"QC 报告检测的是资产 {report.asset_version_id}，"
                    "而该资产不在本清单的 asset_refs 中（终检证据与交付物不对应）"
                ),
            )
        return EvidenceItem(
            EvidenceRefType.QC_REPORT, qc_ref, EvidenceState.VERIFIED, facts=facts
        )

    async def _resolve_spec(
        self, spec_ref: str, *, manifest: DeliveryManifest
    ) -> EvidenceItem:
        """规格证据：经所有者域端口解析（ADR-R28），不直读他域表。"""
        spec = await self._spec_evidence.resolve(
            tenant_id=manifest.tenant_id,
            organization_id=manifest.organization_id,
            spec_id=spec_ref,
        )
        if spec is None:
            return EvidenceItem(
                EvidenceRefType.SHOT_SPEC,
                spec_ref,
                EvidenceState.MISSING,
                reason="引用的 ShotSpec 不可见或不存在",
            )
        facts = {
            "project_id": spec.project_id,
            "shot_id": spec.shot_id,
            "status": spec.status,
            "entity_version": spec.entity_version,
        }
        if spec.project_id != manifest.project_id:
            return EvidenceItem(
                EvidenceRefType.SHOT_SPEC,
                spec_ref,
                EvidenceState.MISMATCHED,
                facts=facts,
                reason=f"规格归属项目 {spec.project_id}，与清单项目 {manifest.project_id} 不一致",
            )
        return EvidenceItem(
            EvidenceRefType.SHOT_SPEC, spec_ref, EvidenceState.VERIFIED, facts=facts
        )

    def _unmodeled_refs(self, manifest: DeliveryManifest) -> list[EvidenceItem]:
        """Phase 1 尚未建模的两类证据：显式标记为不可校验。

        为什么不跳过：跳过等于"默认它成立"。PictureLock 与权利证据恰恰是
        交付里最不能默认成立的两类（一个锁画面，一个锁版权），
        所以它们必须作为 `NOT_MODELED` 出现在报告里，让 `complete=False`
        成为可解释的事实。
        """
        items = [
            EvidenceItem(
                EvidenceRefType.PICTURE_LOCK,
                manifest.picture_lock_ref,
                EvidenceState.NOT_MODELED,
                reason=(
                    "PictureLock 在 Phase 1 尚未建模（ADR-R33 §3 债务 2），"
                    "只能记录引用、无法校验其成立"
                ),
            )
        ]
        items.extend(
            EvidenceItem(
                EvidenceRefType.RIGHTS_EVIDENCE,
                ref,
                EvidenceState.NOT_MODELED,
                reason=(
                    "RightsManifest 在 Phase 1 尚未建模（ADR-R24 债务），"
                    "权利证据无法逐项校验"
                ),
            )
            for ref in manifest.rights_evidence_refs
        )
        return items


__all__ = [
    "DeliveryManifestRebuildService",
    "EvidenceItem",
    "EvidenceRefType",
    "EvidenceState",
    "ManifestRebuildReport",
]
