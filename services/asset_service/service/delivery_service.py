"""交付清单应用服务（DELIVERY.MANIFEST.CREATE）。

职责：把"生成一份可重建的 DRAFT 交付清单"这件事做完整——
校验证据引用、算校验和、写清单、发事件；不负责审批与放行。

未在这里做的事（刻意，且属显式债务）：
- 不判断 Gate4 是否 PASS：GateDecision 属编排域，本服务不能跨域读它的表，
  也不允许"凭客户端声称的 gate_pass=true 就放行"。清单只能引用**证据**，
  结论由审批动作（`DELIVERY.MANIFEST.APPROVE`）承担，该动作尚未实现。
- 不把清单推进到 VALIDATED/RELEASED：那需要 PictureLock、权利证据、终检报告
  齐备的审批链，属 Phase 1 后续主链。
"""

from __future__ import annotations

import uuid

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.errors.repository_errors import DuplicateEntityError
from packages.identity.principal import Principal
from packages.shared.outbox_writer import OutboxWriter
from services.asset_service.domain.delivery_manifest import DeliveryManifest
from services.asset_service.domain.enums import AssetStatus, DeliveryManifestStatus
from services.asset_service.repository.asset_repo import AssetRepository
from services.asset_service.repository.manifest_repo import (
    DeliveryManifestRepository,
)

#: 允许进入交付清单的资产状态。
#:
#: 只认 APPROVED 与 GOLD：APPROVED 表示已过人工复核，GOLD 是"已验证的基准版本"。
#: 把 GENERATED/TECHNICAL_QC 也放进来，等于允许"还没审的资产"出现在对客户的
#: 交付清单上——这是交付类缺陷里最难追回的一种（客户已经看到了）。
_DELIVERABLE_STATES = frozenset({AssetStatus.APPROVED, AssetStatus.GOLD})


class DeliveryService:
    """交付清单用例（单域事务，无跨服务依赖）。"""

    def __init__(
        self,
        assets: AssetRepository,
        manifests: DeliveryManifestRepository,
        outbox: OutboxWriter,
    ) -> None:
        self._assets = assets
        self._manifests = manifests
        self._outbox = outbox

    async def create_manifest(
        self,
        *,
        project_id: str,
        spec_ref: str,
        qc_final_ref: str,
        picture_lock_ref: str,
        asset_refs: list[str],
        rights_evidence_refs: list[str],
        rebuild_recipe: str,
        principal: Principal,
        manifest_id: str | None = None,
    ) -> DeliveryManifest:
        """生成 DRAFT 交付清单（幂等：同 id 同内容返回既有清单）。"""
        new_manifest_id = manifest_id or uuid.uuid4().hex

        manifest = DeliveryManifest(
            manifest_id=new_manifest_id,
            project_id=project_id,
            asset_refs=asset_refs,
            spec_ref=spec_ref,
            qc_final_ref=qc_final_ref,
            picture_lock_ref=picture_lock_ref,
            rights_evidence_refs=rights_evidence_refs,
            rebuild_recipe=rebuild_recipe,
            created_by=principal.subject_id.strip(),
            tenant_id=principal.tenant_id,
            organization_id=principal.organization_id,
            status=DeliveryManifestStatus.DRAFT,
        )

        # 快速路径：同 id 已存在时按内容是否一致决定"幂等返回"还是"拒绝覆盖"。
        existing = await self._manifests.get_by_id(new_manifest_id)
        if existing is not None:
            return _assert_same_content(existing, manifest, new_manifest_id)

        await self._assert_assets_are_deliverable(
            asset_refs=asset_refs, project_id=project_id
        )

        try:
            await self._manifests.add(manifest)
        except DuplicateEntityError as exc:
            # 并发重复提交：回查后同样走内容比对（与快速路径共用同一判定）。
            concurrent = await self._manifests.get_by_id(new_manifest_id)
            if concurrent is not None:
                return _assert_same_content(concurrent, manifest, new_manifest_id)
            raise DomainError(
                ErrorRegistry.CONCURRENT_MODIFICATION,
                detail="交付清单并发写入冲突，请重试",
            ) from exc

        self._outbox.record(
            aggregate_type="DeliveryManifest",
            aggregate_id=manifest.manifest_id,
            tenant_id=principal.tenant_id,
            event_type="DELIVERY_MANIFEST_CREATED",
            payload={
                "manifest_id": manifest.manifest_id,
                "project_id": manifest.project_id,
                "asset_refs": manifest.asset_refs,
                "checksum": manifest.checksum,
                "spec_ref": manifest.spec_ref,
                "qc_final_ref": manifest.qc_final_ref,
                "picture_lock_ref": manifest.picture_lock_ref,
                "rights_evidence_refs": manifest.rights_evidence_refs,
                "status": manifest.status.value,
                "created_by": manifest.created_by,
                "tenant_id": str(principal.tenant_id),
            },
        )
        return manifest

    async def get_manifest(self, manifest_id: str) -> DeliveryManifest:
        """读取清单；不可见即 NOT_FOUND（不区分"不存在"与"无权看"）。"""
        manifest = await self._manifests.get_by_id(manifest_id)
        if manifest is None:
            raise DomainError(
                ErrorRegistry.NOT_FOUND, detail=f"交付清单不存在: {manifest_id}"
            )
        return manifest

    async def _assert_assets_are_deliverable(
        self, *, asset_refs: list[str], project_id: str
    ) -> None:
        """逐个校验被引用的资产版本确实可交付。

        为什么在服务层逐条查而不是"信任引用列表"：清单一旦 VALIDATED 就会成为
        对客户的交付依据。引用一个不存在的版本，交付时才发现缺件；引用一个
        还在 TECHNICAL_QC 的版本，等于把未审资产发出去。两种失败都发生在
        "已经承诺交付"之后，补救成本远高于此刻拒绝。
        """
        for asset_id in asset_refs:
            asset = await self._assets.get_by_id(asset_id)
            if asset is None:
                raise DomainError(
                    ErrorRegistry.VALIDATION_ERROR,
                    detail=f"asset_refs 引用了不存在的资产版本: {asset_id}",
                )
            if asset.project_id != project_id:
                raise DomainError(
                    ErrorRegistry.VALIDATION_ERROR,
                    detail=(
                        f"资产版本 {asset_id} 属于其他项目（{asset.project_id}），"
                        "不得进入本项目的交付清单"
                    ),
                )
            if asset.status not in _DELIVERABLE_STATES:
                raise DomainError(
                    ErrorRegistry.VALIDATION_ERROR,
                    detail=(
                        f"资产版本 {asset_id} 当前状态为 {asset.status.value}，"
                        "只有 APPROVED / GOLD 的版本可进入交付清单"
                    ),
                )


def _assert_same_content(
    existing: DeliveryManifest, incoming: DeliveryManifest, manifest_id: str
) -> DeliveryManifest:
    """幂等键复用必须是同一请求：内容一致才返回既有清单。

    用 `checksum` 而不是逐字段比较：校验和只覆盖可重建内容，正是"同一份交付"
    的定义。内容不同却复用同一 manifest_id 属于**改写已发出的交付清单**，
    契约要求"每个 Manifest 版本不可变"，因此一律拒绝并要求新 id。
    """
    if existing.checksum == incoming.checksum:
        return existing
    raise DomainError(
        ErrorRegistry.IDEMPOTENCY_CONFLICT,
        detail=(
            f"manifest_id {manifest_id} 已存在且内容不同：交付清单不可改写，"
            "请使用新的 manifest_id 生成新版本"
        ),
    )


__all__ = ["DeliveryService"]
