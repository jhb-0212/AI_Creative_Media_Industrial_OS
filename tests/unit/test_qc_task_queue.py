"""QC 待检队列（Phase 3）的单元测试。

守住的失效模式：
- 重复入队：同一生成任务只能对应一个 QC 任务（否则同一产物被检两次）；
- 空产物引用：没有 asset_version_id 的任务不得入队（否则 QC 检无可检）；
- 无报告判定：PASSED/REJECTED 必须挂 qc_report_id（无证据即假绿）；
- 越权租约：非持有者不得续租/判定（ACT-QC-NEG-003）；
- 崩溃回收：只有过期租约才能退回 QUEUED，未过期不许打断。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.shared.evidence_ports import ReportFacts
from packages.shared.in_memory import InMemoryOutboxWriter, InMemoryRepository
from services.project_planning.domain.enums import QCTaskStatus
from services.qc_governance.domain.qc_task import EnqueueOrigin
from services.qc_governance.service.qc_task_service import QCTaskService

_TENANT = uuid4()


class FakeQCTaskRepository(InMemoryRepository):
    async def find_by_generation_task(self, generation_task_id: str):
        for task in self.snapshot().values():
            if task.generation_task_id == generation_task_id:
                return task
        return None


def _service(
    *,
    report_asset: str = "asset-1",
    report_tenant: UUID | None = None,
    known_report_ids: frozenset[str] | None = None,
) -> tuple[QCTaskService, FakeQCTaskRepository, InMemoryOutboxWriter]:
    """默认装配一个「报告确实存在且属于 asset-1」的核验器。

    核验器不可缺省：``QCTaskService.finish`` 在未装配时 fail-closed，
    这条默认值让多数用例专注在自己要测的语义上；要测核验失败的用例
    显式传 ``report_asset`` / ``known_report_ids``。
    """
    repo = FakeQCTaskRepository()
    outbox = InMemoryOutboxWriter()

    async def lookup(qc_report_id: str, tenant_id: UUID) -> ReportFacts | None:
        if known_report_ids is not None and qc_report_id not in known_report_ids:
            return None
        return ReportFacts(
            asset_version_id=report_asset,
            tenant_id=report_tenant if report_tenant is not None else tenant_id,
        )

    return QCTaskService(repo, outbox, report_lookup=lookup), repo, outbox


async def _enqueued(service: QCTaskService, generation_task_id: str = "gen-1"):
    return await service.enqueue(
        project_id="proj-1",
        asset_version_id="asset-1",
        generation_task_id=generation_task_id,
        quality_profile_version="qp-1",
        tenant_id=_TENANT,
        origin=EnqueueOrigin(source="RELAY", operator_id="outbox-relay"),
        shot_id="shot-1",
    )


async def test_enqueue_creates_queued_and_event() -> None:
    service, _repo, outbox = _service()
    task = await _enqueued(service)
    assert task.status is QCTaskStatus.QUEUED
    assert task.qc_task_id == task.entity_id
    assert outbox.events[-1]["event_type"] == "QC_TASK_QUEUED"
    assert outbox.events[-1]["payload"]["asset_version_id"] == "asset-1"


async def test_duplicate_enqueue_is_business_conflict() -> None:
    service, _repo, _outbox = _service()
    await _enqueued(service)
    with pytest.raises(DomainError) as exc:
        await _enqueued(service)
    assert exc.value.error_code == ErrorRegistry.IDEMPOTENCY_CONFLICT


async def test_enqueue_requires_asset_reference() -> None:
    service, _repo, _outbox = _service()
    with pytest.raises(DomainError):
        await service.enqueue(
            project_id="proj-1",
            asset_version_id="",
            generation_task_id="gen-x",
            quality_profile_version="qp-1",
            tenant_id=_TENANT,
            origin=EnqueueOrigin(source="RELAY", operator_id="outbox-relay"),
        )


async def test_claim_and_second_claim_rejected() -> None:
    service, repo, outbox = _service()
    task = await _enqueued(service)

    claimed = await service.claim(
        task_id=task.entity_id, worker_id="qc-1", lease_seconds=120
    )
    assert claimed.status is QCTaskStatus.RUNNING
    assert claimed.lease_owner == "qc-1"
    assert claimed.attempt_count == 1
    assert outbox.events[-1]["event_type"] == "QC_TASK_STARTED"

    with pytest.raises(DomainError):
        await service.claim(task_id=task.entity_id, worker_id="qc-2", lease_seconds=120)
    assert repo.snapshot()[task.entity_id].lease_owner == "qc-1"


async def test_renew_requires_owner() -> None:
    service, _repo, outbox = _service()
    task = await _enqueued(service)
    await service.claim(task_id=task.entity_id, worker_id="qc-1", lease_seconds=60)
    before = task.lease_expires_at

    with pytest.raises(DomainError):
        await service.renew(task_id=task.entity_id, worker_id="qc-2", lease_seconds=60)

    renewed = await service.renew(
        task_id=task.entity_id, worker_id="qc-1", lease_seconds=300
    )
    assert renewed.lease_expires_at > before
    assert outbox.events[-1]["event_type"] == "QC_TASK_LEASE_RENEWED"


async def test_finish_requires_owner_and_report() -> None:
    service, _repo, outbox = _service()
    task = await _enqueued(service)
    await service.claim(task_id=task.entity_id, worker_id="qc-1", lease_seconds=120)

    with pytest.raises(DomainError):
        await service.finish(
            task_id=task.entity_id,
            worker_id="qc-2",
            result="PASSED",
            qc_report_id="report-1",
        )

    with pytest.raises(DomainError):
        await service.finish(
            task_id=task.entity_id,
            worker_id="qc-1",
            result="PASSED",
            qc_report_id="",
        )

    done = await service.finish(
        task_id=task.entity_id,
        worker_id="qc-1",
        result="PASSED",
        qc_report_id="report-1",
        evidence_refs=["asset-1"],
    )
    assert done.status is QCTaskStatus.PASSED
    assert done.lease_owner == ""
    assert done.qc_report_id == "report-1"
    assert outbox.events[-1]["event_type"] == "QC_TASK_PASSED"


async def test_rejected_path_emits_rejected_event() -> None:
    service, _repo, outbox = _service()
    task = await _enqueued(service)
    await service.claim(task_id=task.entity_id, worker_id="qc-1", lease_seconds=120)
    rejected = await service.finish(
        task_id=task.entity_id,
        worker_id="qc-1",
        result="REJECTED",
        qc_report_id="report-2",
    )
    assert rejected.status is QCTaskStatus.REJECTED
    assert outbox.events[-1]["event_type"] == "QC_TASK_REJECTED"


async def test_release_expired_only_after_expiry() -> None:
    service, repo, _outbox = _service()
    task = await _enqueued(service)
    await service.claim(task_id=task.entity_id, worker_id="qc-1", lease_seconds=120)

    with pytest.raises(DomainError):
        await service.release_expired(task_id=task.entity_id)

    repo.snapshot()[task.entity_id].lease_expires_at = datetime.now(UTC) - timedelta(
        seconds=5
    )
    released = await service.release_expired(task_id=task.entity_id)
    assert released.status is QCTaskStatus.QUEUED
    assert released.lease_owner == ""
    # 领取次数保留：崩溃过一次必须留痕。
    assert released.attempt_count == 1


async def test_finish_rejects_unknown_report_id() -> None:
    """任意编造的 report_id 必须被拒——「有 ID」不等于「有证据」。"""
    service, _repo, _outbox = _service(known_report_ids=frozenset({"real-report"}))
    task = await _enqueued(service)
    await service.claim(task_id=task.entity_id, worker_id="qc-1", lease_seconds=120)

    with pytest.raises(DomainError, match="不存在"):
        await service.finish(
            task_id=task.entity_id,
            worker_id="qc-1",
            result="PASSED",
            qc_report_id="forged-report",
        )

    assert task.status is QCTaskStatus.RUNNING


async def test_finish_rejects_report_of_other_asset_version() -> None:
    """报告必须是本任务引用那份资产的报告：换一个版本同样是伪造证据。"""
    service, _repo, _outbox = _service(report_asset="asset-OTHER")
    task = await _enqueued(service)
    await service.claim(task_id=task.entity_id, worker_id="qc-1", lease_seconds=120)

    with pytest.raises(DomainError, match="不是本任务引用的"):
        await service.finish(
            task_id=task.entity_id,
            worker_id="qc-1",
            result="PASSED",
            qc_report_id="report-1",
        )

    assert task.status is QCTaskStatus.RUNNING


async def test_finish_rejects_report_of_other_tenant() -> None:
    service, _repo, _outbox = _service(report_tenant=uuid4())
    task = await _enqueued(service)
    await service.claim(task_id=task.entity_id, worker_id="qc-1", lease_seconds=120)

    with pytest.raises(DomainError, match="不属于本租户"):
        await service.finish(
            task_id=task.entity_id,
            worker_id="qc-1",
            result="PASSED",
            qc_report_id="report-1",
        )


async def test_finish_without_verifier_fails_closed() -> None:
    """未装配核验器时拒绝终结，而不是「没法验就放行」。

    放行会让「不验证据」成为默认路径，且装配错误只表现为判定照样通过。
    """
    repo = FakeQCTaskRepository()
    service = QCTaskService(repo, InMemoryOutboxWriter())
    task = await _enqueued(service)
    await service.claim(task_id=task.entity_id, worker_id="qc-1", lease_seconds=120)

    with pytest.raises(DomainError, match="报告核验器"):
        await service.finish(
            task_id=task.entity_id,
            worker_id="qc-1",
            result="PASSED",
            qc_report_id="report-1",
        )

    assert task.status is QCTaskStatus.RUNNING
