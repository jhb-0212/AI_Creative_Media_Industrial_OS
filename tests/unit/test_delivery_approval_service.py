"""交付清单审批消费用例单测（内存 fake，无数据库）。

验证 D4 的核心红线（不是字段齐全）：

- **结论只来自权威事件**：没有 APPROVAL_TICKET_COMPLETED/APPROVED 就不推进，
  客户端没有任何入参可声明 gate_pass；
- **终态幂等**：同一事件重放返回同一清单、不重复发事件；不同事件改写终态被拒；
- **并发可控**：乐观锁冲突转领域错误，不吞错；
- **放行前置**：只有 VALIDATED 可 RELEASED，且 RELEASED 幂等；
- **内容不变量**：状态推进不改 checksum / asset_refs。
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.shared.outbox_writer import OutboxWriter
from packages.shared.versioning import ConcurrentModificationError
from services.asset_service.domain.delivery_manifest import DeliveryManifest
from services.asset_service.domain.enums import DeliveryManifestStatus
from services.asset_service.repository.approval_event_source import (
    ApprovalCompletedEvent,
    ApprovalEventSource,
)
from services.asset_service.repository.manifest_repo import (
    DeliveryManifestRepository,
)
from services.asset_service.service.delivery_approval_service import (
    DeliveryApprovalService,
)
from tests.unit.test_asset_service import _principal

_TENANT = uuid.UUID("00000000-0000-0000-0000-0000000000d4")
_PROJECT = "proj-delivery-approval"
_APPROVE_KEY = "DELIVERY.MANIFEST.APPROVE"
_RESOURCE_TYPE = "DeliveryManifest"


def _manifest(**overrides: object) -> DeliveryManifest:
    params: dict[str, object] = {
        "manifest_id": "manifest-1",
        "project_id": _PROJECT,
        "asset_refs": ["asset-a"],
        "spec_ref": "spec-1",
        "qc_final_ref": "qc-final-1",
        "picture_lock_ref": "picture-lock-1",
        "rights_evidence_refs": ["rights-1"],
        "rebuild_recipe": "recipe://spec-1/v1",
        "created_by": "delivery_1",
        "tenant_id": _TENANT,
    }
    params.update(overrides)
    return DeliveryManifest(**params)  # type: ignore[arg-type]


def _event(
    *,
    event_id: uuid.UUID = uuid.UUID("00000000-0000-0000-0000-0000000000e1"),
    decision: str = "APPROVED",
    resource_id: str = "manifest-1",
    project_id: str | None = _PROJECT,
) -> ApprovalCompletedEvent:
    return ApprovalCompletedEvent(
        event_id=event_id,
        action_key=_APPROVE_KEY,
        resource_type=_RESOURCE_TYPE,
        resource_id=resource_id,
        decision=decision,
        votes=(
            {"group_name": "delivery_owner", "decision": "APPROVE"},
            {"group_name": "qc_rights", "decision": "APPROVE"},
        ),
        occurred_at=datetime.now(UTC),
        tenant_id=_TENANT,
        project_id=project_id,
    )


class FakeManifestRepo(DeliveryManifestRepository):
    """内存仓储：save_status 模拟乐观锁与四列更新。"""

    def __init__(self, manifest: DeliveryManifest) -> None:
        self.tickets: dict[str, DeliveryManifest] = {manifest.manifest_id: manifest}
        self.save_calls = 0
        self.conflict = False

    async def add(self, manifest: DeliveryManifest) -> None:
        self.tickets[manifest.manifest_id] = manifest

    async def get_by_id(self, manifest_id: str) -> DeliveryManifest | None:
        return self.tickets.get(manifest_id)

    async def save_status(
        self, manifest: DeliveryManifest, *, expected_version: int
    ) -> None:
        self.save_calls += 1
        if self.conflict:
            raise ConcurrentModificationError(
                f"delivery_manifest:{manifest.manifest_id}", expected_version
            )
        current = self.tickets.get(manifest.manifest_id)
        if current is None or current.entity_version != expected_version:
            raise ConcurrentModificationError(
                f"delivery_manifest:{manifest.manifest_id}", expected_version
            )
        self.tickets[manifest.manifest_id] = replace(manifest)


class FakeEventSource(ApprovalEventSource):
    def __init__(self, event: ApprovalCompletedEvent | None) -> None:
        self.event = event

    async def find_completed(
        self, *, action_key: str, resource_type: str, resource_id: str
    ) -> ApprovalCompletedEvent | None:
        if (
            self.event is not None
            and self.event.action_key == action_key
            and self.event.resource_type == resource_type
            and self.event.resource_id == resource_id
        ):
            return self.event
        return None

    async def list_completed(
        self, *, action_key: str, resource_type: str
    ) -> tuple[ApprovalCompletedEvent, ...]:
        return (self.event,) if self.event is not None else ()


class FakeOutbox(OutboxWriter):
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def record(
        self,
        *,
        aggregate_type: str,
        aggregate_id: str,
        tenant_id: uuid.UUID,
        event_type: str,
        payload: dict[str, Any],
    ) -> uuid.UUID:
        self.events.append({"event_type": event_type, "payload": payload})
        return uuid.uuid4()


def _service(
    *,
    manifest: DeliveryManifest | None = None,
    event: ApprovalCompletedEvent | None = None,
) -> tuple[DeliveryApprovalService, FakeManifestRepo, FakeOutbox]:
    repo = FakeManifestRepo(manifest or _manifest())
    outbox = FakeOutbox()
    service = DeliveryApprovalService(repo, FakeEventSource(event), outbox)
    return service, repo, outbox


async def test_approved_event_promotes_draft_to_validated() -> None:
    """APPROVED 权威事件把 DRAFT 连推到 VALIDATED，发一个 APPROVED 事件。"""
    event = _event()
    service, repo, outbox = _service(event=event)
    result = await service.consume_approval(manifest_id="manifest-1")
    assert result.status is DeliveryManifestStatus.VALIDATED
    assert result.audit_ref == str(event.event_id)
    assert result.entity_version == 3  # DRAFT→VALIDATING→VALIDATED 两步
    assert repo.tickets["manifest-1"].status is DeliveryManifestStatus.VALIDATED
    assert [e["event_type"] for e in outbox.events] == ["DELIVERY_MANIFEST_APPROVED"]


async def test_no_event_refuses_to_promote() -> None:
    """没有审批完成事件：拒绝推进，不发事件。"""
    service, repo, outbox = _service(event=None)
    with pytest.raises(DomainError) as exc:
        await service.consume_approval(manifest_id="manifest-1")
    assert exc.value.error_code is ErrorRegistry.VALIDATION_ERROR
    assert repo.save_calls == 0
    assert outbox.events == []


async def test_rejected_event_refuses_to_promote() -> None:
    """票据 REJECTED：不推进，不发 APPROVED 事件。"""
    service, repo, outbox = _service(event=_event(decision="REJECTED"))
    with pytest.raises(DomainError):
        await service.consume_approval(manifest_id="manifest-1")
    assert repo.save_calls == 0
    assert outbox.events == []


async def test_event_project_mismatch_refuses_to_promote() -> None:
    """R4-2：事件项目归属与清单不一致 → 拒绝跨项目推进。"""
    event = _event(project_id="other-project")
    service, repo, outbox = _service(event=event)
    with pytest.raises(DomainError) as exc:
        await service.consume_approval(manifest_id="manifest-1")
    assert exc.value.error_code is ErrorRegistry.VALIDATION_ERROR
    assert repo.save_calls == 0
    assert outbox.events == []


async def test_event_missing_project_refuses_to_promote() -> None:
    """R4-2：事件缺少项目归属（旧事件/伪造）→ 拒绝推进，绝不默认放行。"""
    event = _event(project_id=None)
    service, repo, outbox = _service(event=event)
    with pytest.raises(DomainError) as exc:
        await service.consume_approval(manifest_id="manifest-1")
    assert exc.value.error_code is ErrorRegistry.VALIDATION_ERROR
    assert repo.save_calls == 0
    assert outbox.events == []


async def test_replay_of_same_event_is_idempotent() -> None:
    """同一事件重放：终态直接返回，不重复推进、不重复发事件。"""
    event = _event()
    service, repo, outbox = _service(event=event)
    first = await service.consume_approval(manifest_id="manifest-1")
    saves_after_first = repo.save_calls  # 两步落库（VALIDATING + VALIDATED）
    second = await service.consume_approval(manifest_id="manifest-1")
    assert first.status is second.status is DeliveryManifestStatus.VALIDATED
    # 重放不再增加任何写入——幂等的准确定义是"第二次没有副作用"。
    assert repo.save_calls == saves_after_first
    assert len(outbox.events) == 1


async def test_different_event_cannot_rewrite_terminal_state() -> None:
    """另一事件试图改写已 VALIDATED 的清单：拒绝（终态不可改写）。"""
    validated = _manifest(
        status=DeliveryManifestStatus.VALIDATED,
        audit_ref="event-original",
        entity_version=3,
    )
    other = _event(event_id=uuid.UUID("00000000-0000-0000-0000-0000000000e2"))
    service, _repo, outbox = _service(manifest=validated, event=other)
    with pytest.raises(DomainError) as exc:
        await service.consume_approval(manifest_id="manifest-1")
    assert exc.value.error_code is ErrorRegistry.STATE_TRANSITION_DENIED
    assert outbox.events == []


async def test_concurrent_conflict_becomes_domain_error() -> None:
    """乐观锁冲突转领域错误，不伪装成成功。"""
    service, repo, outbox = _service(event=_event())
    repo.conflict = True
    with pytest.raises(DomainError) as exc:
        await service.consume_approval(manifest_id="manifest-1")
    assert exc.value.error_code is ErrorRegistry.CONCURRENT_MODIFICATION
    assert outbox.events == []


async def test_release_requires_validated() -> None:
    """RELEASE 前置：DRAFT 不可放行。"""
    service, repo, outbox = _service(event=None)
    principal = _principal("owner", "交付负责人")
    with pytest.raises(DomainError) as exc:
        await service.release_manifest(manifest_id="manifest-1", principal=principal)
    assert exc.value.error_code is ErrorRegistry.STATE_TRANSITION_DENIED
    assert repo.save_calls == 0
    assert outbox.events == []


async def test_release_validated_manifest() -> None:
    """VALIDATED → RELEASED，发 RELEASED 事件，记录放行主体。"""
    validated = _manifest(status=DeliveryManifestStatus.VALIDATED, entity_version=3)
    service, repo, outbox = _service(manifest=validated, event=None)
    principal = _principal("owner", "交付负责人")
    result = await service.release_manifest(
        manifest_id="manifest-1", principal=principal
    )
    assert result.status is DeliveryManifestStatus.RELEASED
    assert "owner" in str(result.audit_ref)
    assert [e["event_type"] for e in outbox.events] == ["DELIVERY_MANIFEST_RELEASED"]
    assert repo.tickets["manifest-1"].status is DeliveryManifestStatus.RELEASED


async def test_release_replay_is_idempotent() -> None:
    """RELEASED 幂等：重复放行不再发事件。"""
    released = _manifest(status=DeliveryManifestStatus.RELEASED, entity_version=4)
    service, repo, outbox = _service(manifest=released, event=None)
    principal = _principal("owner", "交付负责人")
    result = await service.release_manifest(
        manifest_id="manifest-1", principal=principal
    )
    assert result.status is DeliveryManifestStatus.RELEASED
    assert repo.save_calls == 0
    assert outbox.events == []


async def test_content_is_unchanged_by_approval() -> None:
    """状态推进绝不改内容：checksum 与 asset_refs 保持原样。"""
    original = _manifest()
    checksum_before = original.checksum
    service, _repo, _outbox = _service(event=_event())
    result = await service.consume_approval(manifest_id="manifest-1")
    assert result.checksum == checksum_before
    assert result.asset_refs == original.asset_refs
    assert result.rebuild_recipe == original.rebuild_recipe


