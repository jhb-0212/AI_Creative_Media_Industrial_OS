"""QC Worker（Phase 3）的单元测试。

关键不变量：没有检测器时不得领取任务（不制造假绿）；有检测器时判定必须带报告；
执行器异常不得写假的 REJECTED，只能让租约过期后交回队列。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from services.project_planning.domain.enums import QCTaskStatus
from workers.qc.worker import QCInspection, QCWorker

_TENANT = uuid4()


class FakeRepo:
    def __init__(self) -> None:
        self.rows: dict[str, object] = {}

    async def find_by_generation_task(self, generation_task_id: str):
        for task in self.rows.values():
            if task.generation_task_id == generation_task_id:
                return task
        return None

    async def add(self, task):
        self.rows[task.entity_id] = task
        return task

    async def save(self, task, expected_version: int):
        self.rows[task.entity_id] = task
        return task

    async def get_by_id(self, entity_id: str):
        return self.rows.get(entity_id)

    async def next_claimable(self):
        queued = [t for t in self.rows.values() if t.status is QCTaskStatus.QUEUED]
        return min(queued, key=lambda t: t.created_at) if queued else None

    async def list_expired(self, now: datetime):
        return [
            t
            for t in self.rows.values()
            if t.status is QCTaskStatus.RUNNING
            and t.lease_expires_at is not None
            and t.lease_expires_at < now
        ]


class FakeInspector:
    def __init__(self, *, configured=True, inspection=None, raise_exc=None, delay=0.0):
        self.configured = configured
        self.inspection = inspection or QCInspection("PASSED", "report-1", ["asset-1"])
        self._raise = raise_exc
        self._delay = delay
        self.calls = 0

    async def inspect(self, task):
        self.calls += 1
        if self._delay:
            import asyncio

            await asyncio.sleep(self._delay)
        if self._raise is not None:
            raise self._raise
        return self.inspection


def _setup(inspector) -> tuple[QCWorker, FakeRepo, object]:
    from packages.shared.in_memory import InMemoryOutboxWriter
    from services.qc_governance.service.qc_task_service import QCTaskService

    repo = FakeRepo()
    service = QCTaskService(repo, InMemoryOutboxWriter())
    worker = QCWorker(
        service=service,
        repository=repo,
        executor=inspector,
        worker_id="qc-worker-1",
        lease_seconds=120,
        idle_sleep=0.05,
    )
    return worker, repo, service


async def _queued(service, generation_task_id="gen-1"):
    return await service.enqueue(
        project_id="proj-1",
        asset_version_id="asset-1",
        generation_task_id=generation_task_id,
        quality_profile_version="qp-1",
        created_by="generation-worker",
        tenant_id=_TENANT,
        shot_id="shot-1",
    )


async def test_unconfigured_inspector_never_claims() -> None:
    worker, repo, service = _setup(FakeInspector(configured=False))
    await _queued(service)
    assert await worker.process_one() is False
    assert repo.rows[next(iter(repo.rows))].status is QCTaskStatus.QUEUED


async def test_configured_inspector_writes_pass_with_report() -> None:
    worker, repo, service = _setup(FakeInspector())
    task = await _queued(service)

    assert await worker.process_one() is True
    done = repo.rows[task.entity_id]
    assert done.status is QCTaskStatus.PASSED
    assert done.qc_report_id == "report-1"
    assert done.lease_owner == ""


async def test_inspector_exception_leaves_task_running_for_reclaim() -> None:
    worker, repo, service = _setup(FakeInspector(raise_exc=RuntimeError("boom")))
    task = await _queued(service)

    assert await worker.process_one() is True
    current = repo.rows[task.entity_id]
    # 不写假的 REJECTED：停 RUNNING，等租约到期回收。
    assert current.status is QCTaskStatus.RUNNING
    assert current.attempt_count == 1


async def test_sweep_reclaims_expired_only() -> None:
    worker, repo, service = _setup(FakeInspector(configured=False))
    fresh = await _queued(service, "gen-fresh")
    await service.claim(task_id=fresh.entity_id, worker_id="qc-1", lease_seconds=120)
    dead = await _queued(service, "gen-dead")
    await service.claim(task_id=dead.entity_id, worker_id="qc-2", lease_seconds=120)
    dead.lease_expires_at = datetime.now(UTC) - timedelta(seconds=10)

    assert await worker.sweep_expired() == 1
    assert repo.rows[fresh.entity_id].status is QCTaskStatus.RUNNING
    assert repo.rows[dead.entity_id].status is QCTaskStatus.QUEUED


def test_qc_result_literal() -> None:
    assert QCInspection("REJECTED", "report-2").result == "REJECTED"
