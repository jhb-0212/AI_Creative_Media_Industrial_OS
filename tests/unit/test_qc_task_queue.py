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
    # 领取成立一份新租约：代次必须从 0 前移到 1，否则调用方拿不到 fencing 凭据。
    assert claimed.lease_generation == 1
    assert outbox.events[-1]["payload"]["lease_generation"] == 1
    assert outbox.events[-1]["event_type"] == "QC_TASK_STARTED"

    with pytest.raises(DomainError):
        await service.claim(task_id=task.entity_id, worker_id="qc-2", lease_seconds=120)
    assert repo.snapshot()[task.entity_id].lease_owner == "qc-1"


async def test_release_then_reclaim_advances_generation_but_heartbeat_does_not() -> None:
    """代次的三条规则：回收前移、重领前移、心跳不动。

    这三条必须一起测。只测「领取代次 +1」的话，「心跳也 +1」这种错误实现仍然
    会让心跳用例通过（它压根没断言心跳后的值），而 Worker 手里的代次会在第二次
    心跳后就失效——现象是「判定总是被拒」，根因却不在判定。
    """
    service, repo, _outbox = _service()
    task = await _enqueued(service)
    await service.claim(task_id=task.entity_id, worker_id="qc-1", lease_seconds=60)
    assert task.lease_generation == 1

    renewed = await service.renew(
        task_id=task.entity_id,
        worker_id="qc-1",
        expected_lease_generation=1,
        lease_seconds=300,
    )
    assert renewed.lease_generation == 1, "心跳不得推进代次"

    repo.snapshot()[task.entity_id].lease_expires_at = datetime.now(UTC) - timedelta(
        seconds=5
    )
    await service.release_expired(task_id=task.entity_id)
    assert task.lease_generation == 2, "回收即撤销租约，代次必须前移"

    await service.claim(task_id=task.entity_id, worker_id="qc-2", lease_seconds=60)
    assert task.lease_generation == 3


async def test_stale_generation_is_rejected_for_renew_and_finish() -> None:
    """旧代次的续租与判定必须被拒——这是 fencing 的核心断言。

    场景就是真实事故形态：Worker 崩溃 → 租约过期 → 另一个 Worker 重领。
    崩溃的那个进程还活着，它手里有两样东西：worker_id（可能相同，容器重调度）
    和旧代次。只比对 owner 时它会通过；只有代次能把它挡住。
    """
    service, repo, _outbox = _service()
    task = await _enqueued(service)
    await service.claim(task_id=task.entity_id, worker_id="qc-1", lease_seconds=60)
    repo.snapshot()[task.entity_id].lease_expires_at = datetime.now(UTC) - timedelta(
        seconds=5
    )
    await service.release_expired(task_id=task.entity_id)
    await service.claim(task_id=task.entity_id, worker_id="qc-1", lease_seconds=60)
    assert task.lease_generation == 3

    with pytest.raises(DomainError, match="租约代次已过期"):
        await service.renew(
            task_id=task.entity_id,
            worker_id="qc-1",
            expected_lease_generation=1,
            lease_seconds=120,
        )
    with pytest.raises(DomainError, match="租约代次已过期"):
        await service.finish(
            task_id=task.entity_id,
            worker_id="qc-1",
            expected_lease_generation=1,
            result="PASSED",
            qc_report_id="report-1",
        )
    assert task.status is QCTaskStatus.RUNNING


async def test_renew_requires_owner() -> None:
    service, _repo, outbox = _service()
    task = await _enqueued(service)
    await service.claim(task_id=task.entity_id, worker_id="qc-1", lease_seconds=60)
    before = task.lease_expires_at

    with pytest.raises(DomainError):
        await service.renew(task_id=task.entity_id, worker_id="qc-2", expected_lease_generation=task.lease_generation, lease_seconds=60)

    renewed = await service.renew(
        task_id=task.entity_id, worker_id="qc-1", expected_lease_generation=task.lease_generation, lease_seconds=300
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
            expected_lease_generation=task.lease_generation,
            result="PASSED",
            qc_report_id="report-1",
        )

    with pytest.raises(DomainError):
        await service.finish(
            task_id=task.entity_id,
            worker_id="qc-1",
            expected_lease_generation=task.lease_generation,
            result="PASSED",
            qc_report_id="",
        )

    done = await service.finish(
        task_id=task.entity_id,
        worker_id="qc-1",
        expected_lease_generation=task.lease_generation,
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
        expected_lease_generation=task.lease_generation,
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
            expected_lease_generation=task.lease_generation,
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
            expected_lease_generation=task.lease_generation,
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
            expected_lease_generation=task.lease_generation,
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
            expected_lease_generation=task.lease_generation,
            result="PASSED",
            qc_report_id="report-1",
        )

    assert task.status is QCTaskStatus.RUNNING


def _counter(name: str, **labels: str) -> float:
    """读 Prometheus 计数的当前值（未出现过的样本按 0 处理）。"""
    from prometheus_client import REGISTRY

    return REGISTRY.get_sample_value(name, labels or None) or 0.0


async def test_reclaim_and_stale_writes_are_counted() -> None:
    """三个计数的口径本身要被盯住：它们一旦算错，监控会给出反向结论。

    这不是「指标存在性」测试，而是口径测试：
    * 回收成功 +1（回收失败不算，否则重试噪声变成 Worker 死亡率）；
    * 迟到代次被拒 +1（按 operation 分开，renew 与 finish 各算各的）；
    * 正常领取/心跳不产生任何 fencing 拒绝（否则计数永远在涨，无法定性）。
    """
    service, repo, _outbox = _service()
    # 计数器是**进程级**的：别的用例已经把值推高过，所以只能比增量。
    renew_base = _counter("qc_fencing_rejected_total", operation="renew")
    finish_base = _counter("qc_fencing_rejected_total", operation="finish")
    lease_base = _counter("qc_lease_expired_total")

    task = await _enqueued(service)
    await service.claim(task_id=task.entity_id, worker_id="qc-1", lease_seconds=60)
    await service.renew(
        task_id=task.entity_id,
        worker_id="qc-1",
        expected_lease_generation=task.lease_generation,
        lease_seconds=60,
    )
    assert _counter("qc_fencing_rejected_total", operation="renew") == renew_base, (
        "正常心跳不得计入 fencing 拒绝"
    )

    repo.snapshot()[task.entity_id].lease_expires_at = datetime.now(UTC) - timedelta(
        seconds=5
    )
    await service.release_expired(task_id=task.entity_id)
    assert _counter("qc_lease_expired_total") == lease_base + 1, (
        "回收成功必须 +1（这就是「Worker 悄悄死掉」的规模）"
    )

    stale = task.lease_generation - 1
    with pytest.raises(DomainError):
        await service.renew(
            task_id=task.entity_id,
            worker_id="qc-1",
            expected_lease_generation=stale,
            lease_seconds=60,
        )
    with pytest.raises(DomainError):
        await service.finish(
            task_id=task.entity_id,
            worker_id="qc-1",
            expected_lease_generation=stale,
            result="PASSED",
            qc_report_id="report-1",
        )
    assert _counter("qc_fencing_rejected_total", operation="renew") == renew_base + 1
    assert _counter("qc_fencing_rejected_total", operation="finish") == finish_base + 1


async def test_finish_is_single_use_within_one_generation() -> None:
    """同一代租约只能提交一次结论。

    这是「外部副作用幂等」在应用层的那一半：重复提交同一个结论（Worker 重试、
    网络超时后补发）必须被状态机挡住，而不是靠下游报告系统去重。挡在这里，
    「一份报告被记成两次判定」在物理上就不可能发生。
    """
    service, _repo, outbox = _service()
    task = await _enqueued(service)
    await service.claim(task_id=task.entity_id, worker_id="qc-1", lease_seconds=120)

    await service.finish(
        task_id=task.entity_id,
        worker_id="qc-1",
        expected_lease_generation=task.lease_generation,
        result="PASSED",
        qc_report_id="report-1",
    )
    events_before = len(outbox.events)

    with pytest.raises(DomainError):
        await service.finish(
            task_id=task.entity_id,
            worker_id="qc-1",
            expected_lease_generation=task.lease_generation,
            result="PASSED",
            qc_report_id="report-1",
        )
    assert len(outbox.events) == events_before, (
        "被拒的重复提交不得产生第二条终态事件"
    )
