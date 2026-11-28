"""GenerationTask 异步租约（Phase 2 §1）的单元测试。

租约的四条边各自对应的失效模式：
- 领取：同一任务不能被两个 Worker 同时领（否则同一镜头被生成两次，费用翻倍）；
- 续租：非持有者不能延长别人的租约（否则持有者可被静默顶掉）；
- 终结：非持有者不能替别人判定成败（否则审计里的执行者全是假的）；
- 到期回收：只有过期才能收回（否则正常执行会被随时打断）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from packages.errors.error_codes import DomainError
from packages.shared.in_memory import InMemoryOutboxWriter, InMemoryRepository
from packages.shared.quality_profile import UNIFIED_HIGH_QUALITY
from services.production_planning.domain.production_route import ProductionRoute
from services.production_planning.service.generation_service import GenerationService
from services.project_planning.domain.enums import GenerationTaskStatus

_TENANT = uuid4()


def _service() -> tuple[GenerationService, InMemoryRepository, InMemoryOutboxWriter]:
    repo = InMemoryRepository()
    outbox = InMemoryOutboxWriter()
    return GenerationService(repo, outbox), repo, outbox


async def _seed_route(repo: InMemoryRepository) -> None:
    """C4：任务创建要求存在归属一致的 VERIFIED 路线，先于 request_generation 落库。"""
    if await repo.get_by_id("route-1") is None:
        await repo.add(
            ProductionRoute(
                entity_id="route-1",
                created_by="test-seeder",
                tenant_id=_TENANT,
                route_id="route-1",
                project_id="proj-1",
                shot_id="shot-1",
                task_ref="task-1",
                route_type="IMAGE_TO_VIDEO",
                capability_ref="cap-1",
                basis_ref="basis-1",
                quality_profile_version=UNIFIED_HIGH_QUALITY,
            )
        )


async def _requested(service: GenerationService):
    await _seed_route(service._repository)
    return await service.request_generation(
        project_id="proj-1",
        shot_id="shot-1",
        shot_revision_id="rev-1",
        production_route_ref="route-1",
        model_adapter_ref="adapter-1",
        constraint_snapshot_id="snap-1",
        budget_ref="budget-1",
        prompt_ref="inline:prompt-1",
        created_by="director",
        tenant_id=_TENANT,
    )


async def test_async_request_stays_requested() -> None:
    service, _repo, outbox = _service()
    task = await _requested(service)
    assert task.status is GenerationTaskStatus.REQUESTED
    assert task.lease_owner == ""
    assert task.lease_expires_at is None
    assert task.attempt_count == 0
    # 创建只登记「已受理」事件，不产生终态事件，也不写状态迁移事实。
    assert [e["event_type"] for e in outbox.events] == ["GENERATION_REQUESTED"]
    assert "from_status" not in outbox.events[0]["payload"]


async def test_claim_moves_to_running_with_lease() -> None:
    service, _repo, outbox = _service()
    task = await _requested(service)

    claimed = await service.claim(task_id=task.entity_id, worker_id="worker-1", lease_seconds=120)

    assert claimed.status is GenerationTaskStatus.RUNNING
    assert claimed.lease_owner == "worker-1"
    assert claimed.lease_expires_at is not None
    assert claimed.attempt_count == 1
    assert outbox.events[-1]["event_type"] == "GENERATION_STARTED"
    assert outbox.events[-1]["payload"]["worker_id"] == "worker-1"


async def test_second_claim_is_rejected() -> None:
    service, _repo, _outbox = _service()
    task = await _requested(service)
    await service.claim(task_id=task.entity_id, worker_id="worker-1", lease_seconds=120)

    with pytest.raises(DomainError):
        await service.claim(task_id=task.entity_id, worker_id="worker-2", lease_seconds=120)


async def test_renew_requires_lease_owner() -> None:
    service, _repo, outbox = _service()
    task = await _requested(service)
    await service.claim(task_id=task.entity_id, worker_id="worker-1", lease_seconds=120)
    first_expiry = task.lease_expires_at

    with pytest.raises(DomainError):
        await service.renew(task_id=task.entity_id, worker_id="worker-2", lease_seconds=120)

    renewed = await service.renew(task_id=task.entity_id, worker_id="worker-1", lease_seconds=300)
    assert renewed.lease_expires_at is not None
    assert first_expiry is not None
    assert renewed.lease_expires_at > first_expiry
    assert outbox.events[-1]["event_type"] == "GENERATION_LEASE_RENEWED"
    # 续租不迁移状态：事件 from/to 相同会被事实表拒绝，所以这里只发版本事件。
    assert renewed.status is GenerationTaskStatus.RUNNING


async def test_finish_failure_requires_lease_owner_and_clears_lease() -> None:
    service, _repo, outbox = _service()
    task = await _requested(service)
    await service.claim(task_id=task.entity_id, worker_id="worker-1", lease_seconds=120)

    with pytest.raises(DomainError):
        await service.finish(
            task_id=task.entity_id,
            worker_id="worker-2",
            result="FAILED",
        )

    done = await service.finish(
        task_id=task.entity_id,
        worker_id="worker-1",
        result="FAILED",
        evidence_ref="gateway-error",
    )
    assert done.status is GenerationTaskStatus.FAILED
    assert done.lease_owner == ""
    assert done.lease_expires_at is None
    assert outbox.events[-1]["event_type"] == "GENERATION_FAILED"
    assert outbox.events[-1]["payload"]["evidence_ref"] == "gateway-error"


async def test_release_requires_expired_lease() -> None:
    service, _repo, _outbox = _service()
    task = await _requested(service)
    await service.claim(task_id=task.entity_id, worker_id="worker-1", lease_seconds=120)

    # 未到期：拒绝回收，否则正常执行会被随时打断。
    with pytest.raises(DomainError):
        await service.release_expired(task_id=task.entity_id)

    # 人为把租约推到过去，模拟 Worker 崩溃后再无人续租。
    task.lease_expires_at = datetime.now(UTC) - timedelta(seconds=5)
    released = await service.release_expired(task_id=task.entity_id)

    assert released.status is GenerationTaskStatus.REQUESTED
    assert released.lease_owner == ""
    assert released.lease_expires_at is None
    # 领取次数不清零：崩溃过一次这件事必须留痕。
    assert released.attempt_count == 1


async def test_reclaim_after_release_increments_attempts() -> None:
    service, _repo, outbox = _service()
    task = await _requested(service)
    await service.claim(task_id=task.entity_id, worker_id="worker-1", lease_seconds=60)
    task.lease_expires_at = datetime.now(UTC) - timedelta(seconds=5)
    await service.release_expired(task_id=task.entity_id)

    again = await service.claim(task_id=task.entity_id, worker_id="worker-2", lease_seconds=60)
    assert again.status is GenerationTaskStatus.RUNNING
    assert again.lease_owner == "worker-2"
    assert again.attempt_count == 2
    assert outbox.events[-1]["payload"]["attempt_count"] == 2


async def test_act_gen_neg_002_finish_without_lease_is_rejected() -> None:
    """ACT-GEN-NEG-002：没有租约（从未领取 / 已被回收）时不能终结任务。"""
    service, _repo, outbox = _service()
    task = await _requested(service)

    with pytest.raises(DomainError):
        await service.finish(
            task_id=task.entity_id,
            worker_id="worker-1",
            result="FAILED",
            evidence_ref="gateway-error",
        )

    assert task.status is GenerationTaskStatus.REQUESTED
    # 只有创建时的受理事件；没有终态事件，证明终结确实被拒绝。
    assert [e["event_type"] for e in outbox.events] == ["GENERATION_REQUESTED"]


async def test_to_dict_exposes_lease_fields() -> None:
    service, _repo, _outbox = _service()
    task = await _requested(service)
    await service.claim(task_id=task.entity_id, worker_id="worker-1", lease_seconds=60)

    data = task.to_dict()
    assert data["lease_owner"] == "worker-1"
    assert isinstance(data["lease_expires_at"], str)
    assert data["attempt_count"] == 1
    assert data["status"] == "RUNNING"


async def test_worker_cannot_report_success_even_with_product_refs() -> None:
    """C3：成功终态只能来自登记回执，服务层不接受任何自报成功。"""
    service, _repo, _outbox = _service()
    task = await _requested(service)
    await service.claim(task_id=task.entity_id, worker_id="worker-1", lease_seconds=120)

    with pytest.raises(DomainError, match="登记回执"):
        await service.finish(
            task_id=task.entity_id,
            worker_id="worker-1",
            result="SUCCEEDED",
            evidence_ref="asset-1",
        )

    # 一次非法调用既不推进终态，也不偷改仍被持有的任务。
    assert task.status is GenerationTaskStatus.RUNNING
    assert task.asset_version_id == ""


async def test_t3_requires_waiting_state_for_success_receipt() -> None:
    """T3 不能从 RUNNING 直接成功；必须先经 T1 进入等待登记态。"""
    service, _repo, _outbox = _service()
    task = await _requested(service)
    await service.claim(task_id=task.entity_id, worker_id="worker-1", lease_seconds=120)

    from services.project_planning.domain.state_machine import (
        ASSET_REGISTRATION_COMPLETED_EVENT,
    )

    with pytest.raises(DomainError, match="等待登记态"):
        task.bind_registration_result(
            "av-1",
            "qp-1",
            "audit-1",
            ASSET_REGISTRATION_COMPLETED_EVENT,
            GenerationTaskStatus.SUCCEEDED,
        )
    assert task.status is GenerationTaskStatus.RUNNING


async def test_failed_needs_no_product_refs() -> None:
    """失败任务不需要产物引用——它不该出现在 QC 队列。"""
    service, _repo, outbox = _service()
    task = await _requested(service)
    await service.claim(task_id=task.entity_id, worker_id="worker-1", lease_seconds=120)

    done = await service.finish(
        task_id=task.entity_id,
        worker_id="worker-1",
        result="FAILED",
        evidence_ref="gateway-error",
    )

    assert done.status is GenerationTaskStatus.FAILED
    assert outbox.events[-1]["event_type"] == "GENERATION_FAILED"
