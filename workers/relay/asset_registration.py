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
from uuid import NAMESPACE_URL, UUID, uuid5

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.identity.principal import Principal
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


def _field(payload: dict[str, object], name: str) -> str:
    return str(payload.get(name) or "").strip()


def parse_requested(event: OutboxEvent) -> RegistrationRequested:
    p = event.payload
    values = (
        "generation_task_id",
        "project_id",
        "attempt_id",
        "locator",
        "sha256",
        "media_type",
    )
    if any(not _field(p, name) for name in values):
        raise ReceiptEventError("ASSET_REGISTRATION_REQUESTED 缺必需字段")
    return RegistrationRequested(
        tenant_id=event.tenant_id,
        generation_task_id=_field(p, "generation_task_id"),
        project_id=_field(p, "project_id"),
        attempt_id=_field(p, "attempt_id"),
        locator=_field(p, "locator"),
        sha256=_field(p, "sha256").lower(),
        media_type=_field(p, "media_type"),
    )


def parse_result(event: OutboxEvent) -> RegistrationResult:
    p = event.payload
    task_id = _field(p, "generation_task_id")
    if not task_id:
        raise ReceiptEventError(f"{event.event_type} 缺 generation_task_id")
    if event.event_type == "ASSET_REGISTRATION_COMPLETED":
        asset_id = _field(p, "asset_version_id")
        if not asset_id:
            raise ReceiptEventError("COMPLETED 事件缺 asset_version_id")
        return RegistrationResult(
            event.tenant_id, task_id, asset_id, "", True
        )
    if event.event_type == "ASSET_REGISTRATION_REJECTED":
        return RegistrationResult(
            event.tenant_id, task_id, "", _field(p, "reason"), False
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


def build_asset_registration_handlers(
    *,
    production_planning_sessionmaker: object,
    asset_sessionmaker: object,
) -> dict[str, EventHandler]:
    """装配 T2/T3；依赖缺失时不注册，事件积压可见而不是假成功。"""

    async def handle_requested(event: OutboxEvent) -> None:
        request = parse_requested(event)
        # 先用生成域事实核验，不信任事件载荷里的归属与摘要。
        async with production_planning_sessionmaker() as session:  # type: ignore[operator]
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
            ):
                raise ReceiptEventError("登记请求的 locator/sha256 与产物意图不一致")
            frozen_quality_profile = task.quality_profile_version

        async with asset_sessionmaker() as session:  # type: ignore[operator]
            await set_tenant_context(session, request.tenant_id)
            receipts = SqlAlchemyAssetRegistrationReceiptRepository(session)
            existing = await receipts.get_by_tenant_task(
                request.tenant_id, request.generation_task_id
            )
            if existing is not None:
                # 重复投递：不新建资产、不改写回执，也不再发一次结果事件。
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
            )
            await receipts.add(receipt)
            writer = SqlAlchemyOutboxWriter(session)
            payload = {
                "generation_task_id": request.generation_task_id,
                "project_id": request.project_id,
                "tenant_id": str(request.tenant_id),
                "attempt_id": request.attempt_id,
            }
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
        async with asset_sessionmaker() as session:  # type: ignore[operator]
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
        async with production_planning_sessionmaker() as session:  # type: ignore[operator]
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
    }


__all__ = [
    "ReceiptEventError",
    "build_asset_registration_handlers",
    "parse_requested",
    "parse_result",
]
