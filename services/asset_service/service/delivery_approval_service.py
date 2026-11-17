"""交付清单审批消费用例（D4：DELIVERY.MANIFEST.APPROVE / RELEASE）。

职责边界（严格遵守工程铁律）：
- **结论来自权威事件，不接受客户端声明**：APPROVE 的结论只从 outbox 的
  APPROVAL_TICKET_COMPLETED 读取。客户端没有任何入口提交 gate_pass / 审批结果，
  因此"未发生就声明通过"在结构上做不到。
- **不跨服务直连**：不调用 workflow 的服务层，只读它写出的 outbox 事件
  （R2）。asset 角色对 outbox_events 有 SELECT 权限，读事件合法。
- **内容不变量不被绕过**：状态推进走 `DeliveryManifest.transition_to()`（领域
  状态机）+ 仓储 `save_status()`（乐观锁，只改四列）；内容列在端口层、
  SQL 授权层、触发器层三处都不可写。
- **不假装完成跨域校验**：清单引用的 PictureLock / 权利证据对象在 Phase 1
  主链尚未实现（报告 F5）。Gate4 PASS 的判定在通用审批引擎里由 qc_rights
  分组投票承载（approval_bindings.yaml），本服务消费的是其裁决结果，
  不直连 qc_governance 的库表（跨模块写/读他人数据被 V2.0.5 第 14 章禁止）。
  证据对象存在性校验待那些域落地后接入，登记在 ADR-R24 未闭合项。

幂等：outbox 是"至少一次"投递。消费以清单状态为准：
- 已 VALIDATED 且 audit_ref == 事件 id → 同一事件重放，直接返回；
- 已 VALIDATED 但 audit_ref 是别的事件 → 不同审批的重复推进，拒绝（终态不可改写）。
"""

from __future__ import annotations

import uuid

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.identity.principal import Principal
from packages.shared.outbox_writer import OutboxWriter
from packages.shared.versioning import ConcurrentModificationError
from services.asset_service.domain.delivery_manifest import DeliveryManifest
from services.asset_service.domain.enums import DeliveryManifestStatus
from services.asset_service.repository.approval_event_source import (
    ApprovalEventSource,
)
from services.asset_service.repository.manifest_repo import (
    DeliveryManifestRepository,
)

#: 目标动作：与 approval_bindings.yaml / action_registry.yaml 逐字一致。
_APPROVE_ACTION_KEY = "DELIVERY.MANIFEST.APPROVE"
_APPROVE_RESOURCE_TYPE = "DeliveryManifest"


class DeliveryApprovalService:
    """交付清单审批消费（APPROVE 由事件驱动，RELEASE 由授权动作驱动）。"""

    def __init__(
        self,
        manifests: DeliveryManifestRepository,
        events: ApprovalEventSource,
        outbox: OutboxWriter,
    ) -> None:
        self._manifests = manifests
        self._events = events
        self._outbox = outbox

    async def consume_approval(self, *, manifest_id: str) -> DeliveryManifest:
        """从 outbox 核验审批通过事件，把清单推进到 VALIDATED。

        不接受任何"审批结果"入参：结论、分组投票都从权威事件读取。找不到
        APPROVED 事件或结论非 APPROVED 时拒绝，清单保持 DRAFT。
        """
        manifest = await self._require_manifest(manifest_id)

        event = await self._events.find_completed(
            action_key=_APPROVE_ACTION_KEY,
            resource_type=_APPROVE_RESOURCE_TYPE,
            resource_id=manifest_id,
        )
        if event is None or event.decision != "APPROVED":
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail=(
                    f"交付清单 {manifest_id} 无已通过的审批事件"
                    "（APPROVAL_TICKET_COMPLETED/APPROVED），不得推进 VALIDATED"
                ),
            )

        audit_ref = str(event.event_id)

        # 幂等：同一事件重放 → 直接返回；不同结论/事件试图再推进终态 → 拒绝。
        if manifest.status is DeliveryManifestStatus.VALIDATED:
            if manifest.audit_ref == audit_ref:
                return manifest
            raise DomainError(
                ErrorRegistry.STATE_TRANSITION_DENIED,
                detail=(
                    f"交付清单 {manifest_id} 已由另一审批（{manifest.audit_ref}）"
                    "推进 VALIDATED，终态不可改写"
                ),
            )
        if manifest.status is not DeliveryManifestStatus.DRAFT:
            raise DomainError(
                ErrorRegistry.STATE_TRANSITION_DENIED,
                detail=(
                    f"交付清单 {manifest_id} 当前状态 {manifest.status.value} "
                    "不可消费审批事件（仅 DRAFT 可进入审批链）"
                ),
            )

        # 一次权威裁决经过两段登记边：DRAFT → VALIDATING → VALIDATED。
        # **每一步都要真实落库**：数据库转移表只登记相邻边，直接写 VALIDATED
        # 会被触发器当作 DRAFT→VALIDATED 跳跃拒绝（集成测试实证过）。
        # VALIDATING 是"审批进行中"的状态投影，不是内存临时值。
        validating = manifest.transition_to(
            DeliveryManifestStatus.VALIDATING, audit_ref=audit_ref
        )
        await self._save(validating, expected_version=manifest.entity_version)

        validated = validating.transition_to(
            DeliveryManifestStatus.VALIDATED, audit_ref=audit_ref
        )
        await self._save(validated, expected_version=validating.entity_version)

        self._outbox.record(
            aggregate_type="DeliveryManifest",
            aggregate_id=manifest_id,
            tenant_id=manifest.tenant_id,
            event_type="DELIVERY_MANIFEST_APPROVED",
            payload={
                "manifest_id": manifest_id,
                "project_id": manifest.project_id,
                "checksum": manifest.checksum,
                "approval_event_id": audit_ref,
                "votes": list(event.votes),
                "status": DeliveryManifestStatus.VALIDATED.value,
                "tenant_id": str(manifest.tenant_id),
            },
        )
        return validated

    async def release_manifest(
        self, *, manifest_id: str, principal: Principal
    ) -> DeliveryManifest:
        """放行已 VALIDATED 的不可变版本 → RELEASED。

        动作授权 ``DELIVERY.MANIFEST.RELEASE``（CRITICAL，交付负责人/授权发行
        主体，OR 语义）由 API 层的 ``authorize`` 完成；这里只核验状态前置条件，
        不接收客户端声称的审批结论。audit_ref 记录发起人，保证"谁放行的"可追。
        """
        manifest = await self._require_manifest(manifest_id)
        if manifest.status is DeliveryManifestStatus.RELEASED:
            return manifest  # 终态幂等
        if manifest.status is not DeliveryManifestStatus.VALIDATED:
            raise DomainError(
                ErrorRegistry.STATE_TRANSITION_DENIED,
                detail=(
                    f"交付清单 {manifest_id} 当前状态 {manifest.status.value}，"
                    "只有 VALIDATED 的不可变版本可放行"
                ),
            )

        audit_ref = f"release:{principal.subject_id}:{uuid.uuid4().hex}"
        released = manifest.transition_to(
            DeliveryManifestStatus.RELEASED, audit_ref=audit_ref
        )
        await self._save(released, expected_version=manifest.entity_version)

        self._outbox.record(
            aggregate_type="DeliveryManifest",
            aggregate_id=manifest_id,
            tenant_id=manifest.tenant_id,
            event_type="DELIVERY_MANIFEST_RELEASED",
            payload={
                "manifest_id": manifest_id,
                "project_id": manifest.project_id,
                "checksum": manifest.checksum,
                "status": DeliveryManifestStatus.RELEASED.value,
                "released_by": principal.subject_id,
                "tenant_id": str(manifest.tenant_id),
            },
        )
        return released

    async def get_manifest(self, manifest_id: str) -> DeliveryManifest:
        return await self._require_manifest(manifest_id)

    async def _require_manifest(self, manifest_id: str) -> DeliveryManifest:
        manifest = await self._manifests.get_by_id(manifest_id)
        if manifest is None:
            raise DomainError(
                ErrorRegistry.NOT_FOUND, detail=f"交付清单不存在: {manifest_id}"
            )
        return manifest

    async def _save(
        self, manifest: DeliveryManifest, *, expected_version: int
    ) -> None:
        """推进状态；并发冲突转为领域错误（终态判断在新版本上重放）。"""
        try:
            await self._manifests.save_status(
                manifest, expected_version=expected_version
            )
        except ConcurrentModificationError as exc:
            raise DomainError(
                ErrorRegistry.CONCURRENT_MODIFICATION,
                detail=f"交付清单 {manifest.manifest_id} 被并发推进，请重试",
            ) from exc


__all__ = ["DeliveryApprovalService"]
