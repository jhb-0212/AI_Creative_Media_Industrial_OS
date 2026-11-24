"""R42 T2/T3 事件处理器：资产域登记与生成域回执终态。

事务边界（ADR-R42 D4）
----------------------
T2 用 asset_service_role 的会话把 AssetVersion、回执与完成/拒绝事件同事务提交；
T3 用 production_planning_role 的会话读回执、推进任务与写终态事件。两者绝不共用
Session，relay 自己的 outbox_relay_role 也不写业务表。跨域只靠 at-least-once 事件。

幂等
----
T2 的幂等键是 ``(tenant_id, generation_task_id)`` 回执，不是资产随机 ID；
T3 在任务已终态时直接返回。重复投递因此不会产生第二个资产或改写终态。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.identity.principal import Principal
from packages.shared.event_payloads import (
    AssetRegistrationCompletedPayload,
    AssetRegistrationConflictPayload,
    AssetRegistrationRejectedPayload,
    AssetRegistrationRequestedPayload,
    EventPayloadError,
    parse_event_payload,
    stamp_event_payload,
)
from packages.shared.port_errors import DuplicateEntityError
from packages.shared.registration_fingerprint import (
    RegistrationFingerprintInput,
    compute_registration_fingerprint,
)
from packages.shared.rls import set_tenant_context
from packages.shared.storage_locator import extension_for_media_type
from services.asset_service.domain.asset_registration_receipt import (
    AssetRegistrationReceipt,
)
from services.asset_service.domain.enums import AssetRegistrationReceiptStatus
from services.asset_service.repository.sqlalchemy_asset_repo import (
    SqlAlchemyAssetRepository,
)
from services.asset_service.repository.sqlalchemy_qc_repo import (
    SqlAlchemyQCReportRepository,
)
from services.asset_service.repository.sqlalchemy_receipt_repo import (
    SqlAlchemyAssetRegistrationReceiptRepository,
)
from services.asset_service.service.asset_service import AssetService
from services.production_planning.repository.sqlalchemy_repos import (
    SqlAlchemyGenerationTaskRepository,
)
from services.production_planning.service.generation_service import GenerationService
from services.project_planning.domain.enums import (
    ArtifactManifestStatus,
    GenerationTaskStatus,
)
from services.project_planning.repository.sqlalchemy_outbox_writer import (
    SqlAlchemyOutboxWriter,
)
from workers.relay.consumer import EventHandler, OutboxEvent

_SERVICE_ID = "svc:asset-registration"


class ReceiptEventError(ValueError):
    """事件载荷与持久化事实不符；必须失败并最终进死信，不能 ack 掉。"""


@dataclass(frozen=True)
class RegistrationRequested:
    tenant_id: UUID
    generation_task_id: str
    project_id: str
    attempt_id: str
    locator: str
    sha256: str
    media_type: str


@dataclass(frozen=True)
class RegistrationResult:
    tenant_id: UUID
    generation_task_id: str
    asset_version_id: str
    reason: str
    completed: bool


def _parse(event: OutboxEvent, event_type: str) -> object:
    """严格解析事件载荷；违约与业务不一致一样走 relay 失败/死信路径。"""
    try:
        return parse_event_payload(event_type, event.payload)
    except EventPayloadError as exc:
        raise ReceiptEventError(str(exc)) from exc


def parse_requested(event: OutboxEvent) -> RegistrationRequested:
    p = cast(
        AssetRegistrationRequestedPayload,
        _parse(event, "ASSET_REGISTRATION_REQUESTED"),
    )
    return RegistrationRequested(
        tenant_id=event.tenant_id,
        generation_task_id=p.generation_task_id,
        project_id=p.project_id,
        attempt_id=p.attempt_id,
        locator=p.locator,
        sha256=p.sha256.lower(),
        media_type=p.media_type,
    )


def parse_result(event: OutboxEvent) -> RegistrationResult:
    if event.event_type == "ASSET_REGISTRATION_COMPLETED":
        p = cast(
            AssetRegistrationCompletedPayload, _parse(event, event.event_type)
        )
        return RegistrationResult(
            event.tenant_id, p.generation_task_id, p.asset_version_id, "", True
        )
    if event.event_type == "ASSET_REGISTRATION_REJECTED":
        rejected = cast(
            AssetRegistrationRejectedPayload, _parse(event, event.event_type)
        )
        return RegistrationResult(
            event.tenant_id,
            rejected.generation_task_id,
            "",
            rejected.reason,
            False,
        )
    raise ReceiptEventError(f"非登记结果事件: {event.event_type}")


def _receipt_entity_id(tenant_id: UUID, generation_task_id: str) -> str:
    return uuid5(
        NAMESPACE_URL,
        f"asset-registration-receipt:{tenant_id}:{generation_task_id}",
    ).hex


def _asset_entity_id(
    tenant_id: UUID, generation_task_id: str, digest: str
) -> str:
    return uuid5(
        NAMESPACE_URL, f"asset-registration:{tenant_id}:{generation_task_id}:{digest}"
    ).hex


async def _record_conflict(
    session: AsyncSession,
    request: RegistrationRequested,
    canonical: RegistrationFingerprintInput,
    fingerprint: str,
    *,
    expected: str,
) -> None:
    """同事务写冲突审计事件（不建表、不改回执、不建第二资产）。

    事件落在 append-only 的 outbox.outbox_events（无 DELETE 授权），由专用
    no-op handler 正常 ack：证据永久留存，但不抬高 backlog、不被默认 webhook
    外发。expected 为空串表示对方是「无指纹的存量回执」——仍记为冲突，
    因为无法证明两者语义相同（fail-closed）。
    """
    # 幂等：同一 (task, 双指纹) 的冲突只记一次。relay 重投同一冲突请求时，
    # 不产生无限累积的审计事件（写入天然幂等，与资产登记同一原则）。
    # 先查（快路径）+ 异常路径并发双写两条是允许的：都是真实冲突事实，
    # 不丢数据、不改业务状态；不为此引入确定性 event_id 的第二套身份机制。
    already = await session.execute(
        text(
            "SELECT 1 FROM outbox.outbox_events "
            "WHERE event_type = 'ASSET_REGISTRATION_CONFLICT' "
            "AND tenant_id = :tid "
            "AND payload->>'generation_task_id' = :task "
            "AND payload->>'expected_fingerprint' = :expected "
            "AND payload->>'observed_fingerprint' = :observed LIMIT 1"
        ),
        {
            "tid": request.tenant_id,
            "task": request.generation_task_id,
            "expected": expected,
            "observed": fingerprint,
        },
    )
    if already.first() is not None:
        return

    # 回执只存指纹、不存原始字段，无法反推「哪个字段不同」——不伪造 conflict_fields。
    # 证据记双指纹 + 本次 observed 全量。原始登记语义的逐字段值不在回执里：
    # locator/sha256/media_type 可从 T1 的 ASSET_REGISTRATION_REQUESTED（按
    # generation_task_id + attempt_id 定位）查得，质量策略取冻结任务；人工比对时
    # 据此与 observed 逐字段核对。这是最小审计事实，不复制一套原始字段镜像。
    writer = SqlAlchemyOutboxWriter(session)
    writer.record(
        aggregate_type="AssetRegistrationReceipt",
        aggregate_id=_receipt_entity_id(
            request.tenant_id, request.generation_task_id
        ),
        tenant_id=request.tenant_id,
        event_type="ASSET_REGISTRATION_CONFLICT",
        payload=stamp_event_payload(
            {
                "generation_task_id": request.generation_task_id,
                "project_id": request.project_id,
                "tenant_id": str(request.tenant_id),
                "expected_fingerprint": expected,
                "observed_fingerprint": fingerprint,
                "observed": {
                    name: str(value) for name, value in canonical.items()
                },
            }
        ),
    )


async def _consume_conflict(event: OutboxEvent) -> None:
    """冲突事件的专用终态 handler：不驱动任何业务动作，成功返回即 ack。

    事件本身已在 append-only 表中永久留存，是审计事实；这里只需让 relay 正常
    写 published_at，避免把它留在待投递队列里抬高 backlog 或被默认 webhook 误发。
    没有业务动作不等于没有消费边界：载荷仍须严格校验，坏事件进死信而不是被
    「no-op 成功」洗掉。
    """
    cast(AssetRegistrationConflictPayload, _parse(event, event.event_type))
    return None


def build_asset_registration_handlers(
    *,
    production_planning_sessionmaker: async_sessionmaker[AsyncSession],
    asset_sessionmaker: async_sessionmaker[AsyncSession],
) -> dict[str, EventHandler]:
    """装配 T2/T3；依赖缺失时不注册，事件积压可见而不是假成功。"""

    async def handle_requested(event: OutboxEvent) -> None:
        request = parse_requested(event)
        # 先用生成域事实核验，不信任事件载荷里的归属与摘要。
        async with production_planning_sessionmaker() as session:
            await set_tenant_context(session, request.tenant_id)
            repo = SqlAlchemyGenerationTaskRepository(session)
            task = await repo.get_by_generation_task_id(request.generation_task_id)
            if task is None:
                raise ReceiptEventError("登记请求的来源生成任务不存在")
            if task.status is not GenerationTaskStatus.AWAITING_ASSET_REGISTRATION:
                raise ReceiptEventError(
                    f"来源任务状态为 {task.status.value}，不是等待登记态"
                )
            if task.project_id != request.project_id or task.tenant_id != request.tenant_id:
                raise ReceiptEventError("登记请求的项目/租户与来源任务不一致")
            manifest = await repo.get_manifest_by_task_attempt(
                request.generation_task_id, request.attempt_id
            )
            if manifest is None:
                raise ReceiptEventError("登记请求的产物意图不存在")
            if manifest.status is not ArtifactManifestStatus.STORED_VERIFIED:
                raise ReceiptEventError("产物意图尚未确认落盘")
            if (
                manifest.locator != request.locator
                or manifest.sha256 != request.sha256
                or manifest.media_type != request.media_type
            ):
                # media_type 与 locator 扩展名双射，但事件载荷可被篡改；落盘意图里
                # 的 media_type 是 T0 固化事实，逐字比对才能挡住「换类型不换定位符」。
                raise ReceiptEventError(
                    "登记请求的 locator/sha256/media_type 与产物意图不一致"
                )
            # 质量策略取冻结任务，不信事件自报（D7）。其余字段都已与上源事实核验过，
            # 这一份规范化输入既是指纹来源，也是冲突证据里的 observed 值。
            frozen_quality_profile = task.quality_profile_version or ""
            canonical: RegistrationFingerprintInput = {
                "tenant_id": request.tenant_id,
                "generation_task_id": request.generation_task_id,
                "project_id": request.project_id,
                "attempt_id": request.attempt_id,
                "locator": request.locator,
                "sha256": request.sha256,
                "media_type": request.media_type,
                "quality_profile_version": frozen_quality_profile,
            }
            fingerprint = compute_registration_fingerprint(**canonical)

        async with asset_sessionmaker() as session:
            await set_tenant_context(session, request.tenant_id)
            receipts = SqlAlchemyAssetRegistrationReceiptRepository(session)
            existing = await receipts.get_by_tenant_task(
                request.tenant_id, request.generation_task_id
            )

            if existing is not None:
                # 回执已存在：只有「语义完全相同」才算幂等重放。
                # 存量回执指纹为 NULL（指纹机制上线前创建），无法比对——fail-closed，
                # 记冲突而不当作幂等，绝不静默 ACK 一个无法验证来源的重复请求。
                if existing.registration_fingerprint == fingerprint:
                    await session.commit()
                    return
                await _record_conflict(
                    session, request, canonical, fingerprint,
                    expected=existing.registration_fingerprint,
                )
                await session.commit()
                return

            principal = Principal(
                subject_id=_SERVICE_ID,
                tenant_id=request.tenant_id,
                roles=frozenset({"asset_registration"}),
            )
            asset_id = _asset_entity_id(
                request.tenant_id, request.generation_task_id, request.sha256
            )
            asset_type = (
                extension_for_media_type(request.media_type) or "bin"
            ).upper()
            service = AssetService(
                SqlAlchemyAssetRepository(session),
                SqlAlchemyQCReportRepository(session),
                SqlAlchemyOutboxWriter(session),
            )
            rejected_reason = ""
            try:
                asset = await service.create_version(
                    project_id=request.project_id,
                    asset_type=asset_type,
                    sha256=request.sha256,
                    storage_ref=request.locator,
                    source_asset_version_ids=[],
                    rights_manifest_ref=None,
                    adapter_id=None,
                    tool_product=None,
                    operator_ref=_SERVICE_ID,
                    version=frozen_quality_profile or None,
                    principal=principal,
                    entity_id=asset_id,
                )
            except Exception as exc:
                # 永久业务冲突才写拒绝回执；基础设施故障要留给 relay 退避重试，
                # 否则一次短暂数据库抖动会把任务永久判死。
                if not isinstance(exc, DomainError) or exc.error_code not in (
                    ErrorRegistry.IDEMPOTENCY_CONFLICT,
                    ErrorRegistry.VALIDATION_ERROR,
                ):
                    raise
                rejected_reason = f"{type(exc).__name__}: {exc}"

            completed = not rejected_reason
            receipt = AssetRegistrationReceipt(
                entity_id=_receipt_entity_id(
                    request.tenant_id, request.generation_task_id
                ),
                created_by=_SERVICE_ID,
                tenant_id=request.tenant_id,
                generation_task_id=request.generation_task_id,
                project_id=request.project_id,
                asset_version_id=asset.entity_id if completed else "",
                message=rejected_reason,
                status=(
                    AssetRegistrationReceiptStatus.COMPLETED
                    if completed
                    else AssetRegistrationReceiptStatus.REJECTED
                ),
                registration_fingerprint=fingerprint,
            )
            try:
                await receipts.add(receipt)
            except DuplicateEntityError:
                # 并发：另一副本已插入同幂等键回执。回读并转指纹比对，
                # 而不是冒泡死信（根因报告 G-3）。本事务此前创建的资产是确定性
                # entity_id，与对方同一资产；回执插入失败后该行不落库，无第二资产。
                concurrent = await receipts.get_by_tenant_task(
                    request.tenant_id, request.generation_task_id
                )
                if concurrent is None:
                    # 唯一键冲突却读不到回执：异常状态，fail-closed 交给 relay 重试。
                    raise
                if concurrent.registration_fingerprint != fingerprint:
                    await _record_conflict(
                        session, request, canonical, fingerprint,
                        expected=concurrent.registration_fingerprint,
                    )
                await session.commit()
                return
            writer = SqlAlchemyOutboxWriter(session)
            payload = stamp_event_payload(
                {
                    "generation_task_id": request.generation_task_id,
                    "project_id": request.project_id,
                    "tenant_id": str(request.tenant_id),
                    "attempt_id": request.attempt_id,
                }
            )
            if completed:
                writer.record(
                    aggregate_type="AssetRegistrationReceipt",
                    aggregate_id=receipt.entity_id,
                    tenant_id=request.tenant_id,
                    event_type="ASSET_REGISTRATION_COMPLETED",
                    payload={
                        **payload,
                        "asset_version_id": asset.entity_id,
                        "quality_profile_version": frozen_quality_profile,
                    },
                )
            else:
                writer.record(
                    aggregate_type="AssetRegistrationReceipt",
                    aggregate_id=receipt.entity_id,
                    tenant_id=request.tenant_id,
                    event_type="ASSET_REGISTRATION_REJECTED",
                    payload={**payload, "reason": rejected_reason},
                )
            await session.commit()

    async def _consume_result(event: OutboxEvent) -> None:
        result = parse_result(event)
        async with asset_sessionmaker() as session:
            await set_tenant_context(session, result.tenant_id)
            receipt = await SqlAlchemyAssetRegistrationReceiptRepository(
                session
            ).get_by_tenant_task(result.tenant_id, result.generation_task_id)
            if receipt is None:
                raise ReceiptEventError("登记结果事件找不到对应回执")
            expected = (
                AssetRegistrationReceiptStatus.COMPLETED
                if result.completed
                else AssetRegistrationReceiptStatus.REJECTED
            )
            if receipt.status is not expected:
                raise ReceiptEventError(
                    f"事件 {event.event_type} 与回执状态 {receipt.status.value} 矛盾"
                )
            if result.completed and receipt.asset_version_id != result.asset_version_id:
                raise ReceiptEventError("事件的资产 ID 与回执不一致")
            asset_version_id = receipt.asset_version_id
            reason = receipt.message
        async with production_planning_sessionmaker() as session:
            await set_tenant_context(session, result.tenant_id)
            service = GenerationService(
                SqlAlchemyGenerationTaskRepository(session),
                SqlAlchemyOutboxWriter(session),
            )
            await service.complete_from_receipt(
                generation_task_id=result.generation_task_id,
                asset_version_id=asset_version_id,
                reason=reason,
            )
            await session.commit()

    return {
        "ASSET_REGISTRATION_REQUESTED": handle_requested,
        "ASSET_REGISTRATION_COMPLETED": _consume_result,
        "ASSET_REGISTRATION_REJECTED": _consume_result,
        # 冲突审计事件的专用终态 handler：不改业务状态，只让 relay 正常 ack，
        # 事件永久留在 append-only outbox 表作证据（不被默认 webhook 误发）。
        "ASSET_REGISTRATION_CONFLICT": _consume_conflict,
    }


__all__ = [
    "ReceiptEventError",
    "build_asset_registration_handlers",
    "parse_requested",
    "parse_result",
]
