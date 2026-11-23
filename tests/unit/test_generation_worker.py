"""生成 Worker（Phase 2 §3）的单元测试。

只测调度语义，不测真实生成：执行器是注入的假实现。
四条必须守住的失效模式：
- 没任务时不假装干活（返回 False，不产生任何状态变更）；
- 执行器抛异常也必须落 FAILED，不能让任务卡在 RUNNING；
- 心跳失败（租约被抢/被回收）后放弃回写，而不是覆盖别人的结果；
- 扫尾只回收过期租约，未过期的不许动。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from packages.shared.in_memory import InMemoryOutboxWriter, InMemoryRepository
from services.production_planning.service.generation_service import GenerationService
from services.project_planning.domain.enums import GenerationTaskStatus
from workers.generation.worker import GenerationOutcome, GenerationWorker, LeaseLostError

_TENANT = uuid4()


class FakeTaskRepository(InMemoryRepository):
    """在内存仓储上补 Worker 需要的两条查询（真实现走 SQL 索引）。"""

    async def next_claimable(self):
        requested = [
            task
            for task in self.snapshot().values()
            if task.status is GenerationTaskStatus.REQUESTED
        ]
        if not requested:
            return None
        return min(requested, key=lambda task: task.created_at)

    async def list_expired(self, now: datetime):
        return [
            task
            for task in self.snapshot().values()
            if task.status is GenerationTaskStatus.RUNNING
            and task.lease_expires_at is not None
            and task.lease_expires_at < now
        ]

    async def list_succeeded_with_asset(self, limit: int = 50):
        succeeded = [
            task
            for task in self.snapshot().values()
            if task.status is GenerationTaskStatus.SUCCEEDED
            and task.asset_version_id
        ]
        return sorted(succeeded, key=lambda task: task.created_at)[:limit]


class RecordingExecutor:
    def __init__(self, outcome: GenerationOutcome | None = None, *, raise_exc=None, delay=0.0):
        # 默认给「合法成功」：SUCCEEDED 必须带产物引用，否则 Worker 会按
        # 执行器违约落 FAILED（见 test_executor_success_without_product_falls_back_to_failed）。
        self.outcome = outcome or GenerationOutcome(
            "SUCCEEDED",
            "asset://out.mp4",
            asset_version_id="asset-ver-1",
            quality_profile_version="qp-ver-1",
        )
        self._raise = raise_exc
        self._delay = delay
        self.calls = 0

    async def execute(self, task) -> GenerationOutcome:
        self.calls += 1
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._raise is not None:
            raise self._raise
        return self.outcome


def _setup(
    executor,
    *,
    lease_seconds: int = 60,
    qc_enqueuer=None,
    qc_lookup=None,
) -> tuple[GenerationWorker, FakeTaskRepository, GenerationService]:
    repo = FakeTaskRepository()
    service = GenerationService(repo, InMemoryOutboxWriter())
    worker = GenerationWorker(
        service=service,
        repository=repo,
        executor=executor,
        worker_id="worker-1",
        lease_seconds=lease_seconds,
        idle_sleep=0.05,
        qc_enqueuer=qc_enqueuer,
        qc_lookup=qc_lookup,
    )
    return worker, repo, service


async def _requested(service: GenerationService, repo: FakeTaskRepository):
    task = await service.request_generation(
        project_id="proj-1",
        shot_id="shot-1",
        shot_revision_id="rev-1",
        production_route_ref="route-1",
        model_adapter_ref="adapter-1",
        constraint_snapshot_id="snap-1",
        budget_ref="budget-1",
        prompt_ref="prompt-1",
        created_by="director",
        tenant_id=_TENANT,
    )
    return task


async def test_idle_cycle_claims_nothing() -> None:
    worker, repo, _service = _setup(RecordingExecutor())
    assert await worker.process_one() is False
    assert repo.snapshot() == {}


async def test_success_writes_terminal_state_and_evidence() -> None:
    worker, repo, service = _setup(RecordingExecutor())
    task = await _requested(service, repo)

    assert await worker.process_one() is True
    done = repo.snapshot()[task.entity_id]
    assert done.status is GenerationTaskStatus.SUCCEEDED
    assert done.lease_owner == ""
    assert done.attempt_count == 1


async def test_executor_exception_marks_failed() -> None:
    worker, repo, service = _setup(RecordingExecutor(raise_exc=RuntimeError("boom")))
    task = await _requested(service, repo)

    assert await worker.process_one() is True
    failed = repo.snapshot()[task.entity_id]
    assert failed.status is GenerationTaskStatus.FAILED
    assert failed.attempt_count == 1


async def test_lease_lost_abandons_result_without_overwriting() -> None:
    """心跳期间租约被别人回收：执行结果不得回写，任务保持 RECLAIMED 后的状态。"""
    worker, repo, service = _setup(RecordingExecutor(delay=0.5), lease_seconds=1)
    task = await _requested(service, repo)

    async def steal() -> None:
        # 等 Worker 进入 RUNNING 后模拟「租约到期被扫尾回收」
        await asyncio.sleep(0.15)
        repo.snapshot()[task.entity_id].lease_expires_at = (
            datetime.now(UTC) - timedelta(seconds=5)
        )
        await service.release_expired(task_id=task.entity_id)

    await asyncio.gather(steal(), worker.process_one())

    current = repo.snapshot()[task.entity_id]
    # 被回收回到 REQUESTED，而不是被旧执行者写成 SUCCEEDED/FAILED
    assert current.status is GenerationTaskStatus.REQUESTED
    assert current.lease_owner == ""


async def test_sweep_only_reclaims_expired() -> None:
    worker, repo, service = _setup(RecordingExecutor())
    fresh = await _requested(service, repo)
    await service.claim(
        task_id=fresh.entity_id, worker_id="worker-1", lease_seconds=120
    )
    dead = await _requested(service, repo)
    await service.claim(
        task_id=dead.entity_id, worker_id="worker-2", lease_seconds=120
    )
    dead.lease_expires_at = datetime.now(UTC) - timedelta(seconds=10)

    reclaimed = await worker.sweep_expired()

    assert reclaimed == 1
    assert repo.snapshot()[fresh.entity_id].status is GenerationTaskStatus.RUNNING
    assert repo.snapshot()[dead.entity_id].status is GenerationTaskStatus.REQUESTED


async def test_run_once_prefers_work_over_sweep() -> None:
    worker, repo, service = _setup(RecordingExecutor())
    task = await _requested(service, repo)
    assert await worker.run_once() is True
    assert repo.snapshot()[task.entity_id].status is GenerationTaskStatus.SUCCEEDED


def test_lease_lost_error_is_runtime_error() -> None:
    assert issubclass(LeaseLostError, RuntimeError)


async def test_success_enqueues_qc_with_asset_reference() -> None:
    """成功产物必须带着权威资产引用入队，QC 才能检对对象。"""
    seen: list[tuple[str, str, str]] = []

    async def enqueue(task, outcome) -> None:
        seen.append((task.entity_id, outcome.asset_version_id, outcome.quality_profile_version))

    executor = RecordingExecutor(
        GenerationOutcome(
            "SUCCEEDED",
            "asset://out.mp4",
            asset_version_id="asset-ver-1",
            quality_profile_version="qp-r16-v1",
        )
    )
    worker, repo, service = _setup(executor, qc_enqueuer=enqueue)
    task = await _requested(service, repo)

    assert await worker.process_one() is True
    assert seen == [(task.entity_id, "asset-ver-1", "qp-r16-v1")]


async def test_success_persists_product_refs_on_task() -> None:
    """产物引用必须落库：只有内存引用的话，漏入队永远无法对账。"""
    executor = RecordingExecutor(
        GenerationOutcome(
            "SUCCEEDED",
            "asset://out.mp4",
            asset_version_id="asset-ver-1",
            quality_profile_version="qp-r16-v1",
        )
    )
    worker, repo, service = _setup(executor)
    task = await _requested(service, repo)

    assert await worker.process_one() is True
    done = repo.snapshot()[task.entity_id]
    assert done.asset_version_id == "asset-ver-1"
    assert done.quality_profile_version == "qp-r16-v1"


async def test_reconcile_enqueues_success_without_qc_task() -> None:
    """成功但没入队的产物，由对账补上；已入队的不重复提交。"""
    enqueued: list[tuple[str, str, str]] = []
    existing = {"gen-existing"}

    async def enqueue(task, outcome) -> None:
        enqueued.append(
            (task.entity_id, outcome.asset_version_id, outcome.quality_profile_version)
        )

    async def lookup(generation_task_id: str) -> bool:
        return generation_task_id in existing

    worker, repo, service = _setup(
        RecordingExecutor(
            GenerationOutcome(
                "SUCCEEDED",
                "asset://out.mp4",
                asset_version_id="asset-ver-1",
                quality_profile_version="qp-r16-v1",
            )
        ),
        qc_enqueuer=enqueue,
        qc_lookup=lookup,
    )
    task = await _requested(service, repo)
    assert await worker.process_one() is True
    enqueued.clear()  # 正常路径已入队，下面只看对账行为

    # 模拟「任务已成功、队列缺项」：清空去重集合，让对账重新发现它
    existing.clear()
    assert await worker.reconcile_qc() == 1
    assert enqueued == [(task.entity_id, "asset-ver-1", "qp-r16-v1")]

    # 已入队（lookup 返回 True）不得重复提交
    existing.add(task.entity_id)
    enqueued.clear()
    assert await worker.reconcile_qc() == 0
    assert enqueued == []


async def test_failed_task_never_enqueues_qc() -> None:
    called: list[str] = []

    async def enqueue(task, outcome) -> None:
        called.append(task.entity_id)

    worker, repo, service = _setup(
        RecordingExecutor(raise_exc=RuntimeError("boom")), qc_enqueuer=enqueue
    )
    await _requested(service, repo)

    assert await worker.process_one() is True
    assert called == []


async def test_success_without_asset_reference_skips_qc() -> None:
    """没有权威产物引用就不猜：宁可留日志，也不给 QC 一个错对象。"""
    called: list[str] = []

    async def enqueue(task, outcome) -> None:
        called.append(task.entity_id)

    worker, repo, service = _setup(
        RecordingExecutor(GenerationOutcome("SUCCEEDED", "asset://out.mp4")),
        qc_enqueuer=enqueue,
    )
    await _requested(service, repo)

    assert await worker.process_one() is True
    assert called == []


async def test_executor_success_without_product_falls_back_to_failed() -> None:
    """执行器声明 SUCCEEDED 却未给产物 → 必须落 FAILED，不能落 SUCCEEDED。

    无产物的成功会让 QC 入队与漏入队对账拿到一个查不到资产的任务。
    这里也不用「停在 RUNNING 等租约过期」兜底：过期回收会把同一个违约执行器
    无限重试，问题只表现为一条含糊的 finish rejected 日志，永远不显式失败。
    """
    worker, repo, service = _setup(
        RecordingExecutor(GenerationOutcome("SUCCEEDED", "asset://out.mp4"))
    )
    task = await _requested(service, repo)

    assert await worker.process_one() is True

    done = repo.snapshot()[task.entity_id]
    assert done.status is GenerationTaskStatus.FAILED
    assert done.asset_version_id == ""
    assert done.lease_owner == ""


async def test_executor_success_with_product_refs_succeeds() -> None:
    """对照组：产物齐全时照常成功，证明上一条不是「一律失败」。"""
    worker, repo, service = _setup(
        RecordingExecutor(
            GenerationOutcome(
                "SUCCEEDED",
                "asset://out.mp4",
                asset_version_id="av-9",
                quality_profile_version="qp-9",
            )
        )
    )
    task = await _requested(service, repo)

    assert await worker.process_one() is True

    done = repo.snapshot()[task.entity_id]
    assert done.status is GenerationTaskStatus.SUCCEEDED
    assert done.asset_version_id == "av-9"
    assert done.quality_profile_version == "qp-9"
