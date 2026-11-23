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
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest

from packages.errors.error_codes import DomainError
from packages.shared.in_memory import InMemoryOutboxWriter, InMemoryRepository
from services.production_planning.service.generation_service import GenerationService
from services.project_planning.domain.enums import GenerationTaskStatus
from workers.generation.worker import GenerationOutcome, GenerationWorker, LeaseLostError

_TENANT = uuid4()


class FakeTaskRepository(InMemoryRepository):
    """在内存仓储上补 Worker 需要的两条查询（真实现走 SQL 索引）。

    额外记录「作用域打开次数」与「提交次数」：短事务改造的判据是每个阶段各自
    开一个作用域并提交，而不是全程共用一个——这两组计数就是它的可观测形式。
    """

    def __init__(self) -> None:
        super().__init__()
        self.opened = 0
        self.commits: list[str] = []

    async def get_by_generation_task_id(self, generation_task_id: str):
        return next(
            (
                task
                for task in self.snapshot().values()
                if task.generation_task_id == generation_task_id
            ),
            None,
        )

    async def next_claimable_for_claim(self):
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
        # 成功执行器只表示「T1 已在自己的作用域里提交」；Worker 不再写终态，
        # 也不再接收资产引用。
        self.outcome = outcome or GenerationOutcome("SUCCEEDED", "asset://out.mp4")
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


@dataclass
class _Unit:
    """内存版短工作单元：提交只是记账，没有真实事务可提交。"""

    service: Any
    repository: FakeTaskRepository

    async def commit(self) -> None:
        self.repository.commits.append("commit")


def _scope_factory(repo: FakeTaskRepository, service: GenerationService):
    @asynccontextmanager
    async def scope():
        repo.opened += 1
        yield _Unit(service=service, repository=repo)

    return scope


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
        scope=_scope_factory(repo, service),
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


async def test_success_does_not_write_terminal_state() -> None:
    """Worker 成功只代表 T1 已提交；终态由 T3 依据回执推进。"""
    worker, repo, service = _setup(RecordingExecutor())
    task = await _requested(service, repo)

    assert await worker.process_one() is True
    done = repo.snapshot()[task.entity_id]
    # 假执行器没有真的走 T1，所以任务仍停在 RUNNING——关键是 Worker 没有自报成功。
    assert done.status is GenerationTaskStatus.RUNNING
    assert done.asset_version_id == ""
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
    assert repo.snapshot()[task.entity_id].status is GenerationTaskStatus.RUNNING


def test_lease_lost_error_is_runtime_error() -> None:
    assert issubclass(LeaseLostError, RuntimeError)


async def test_success_does_not_enqueue_qc_directly() -> None:
    """成功**不再**直接写 QC 表：入队只能由 GENERATION_SUCCEEDED 事件触发。

    直接写会出现「QCTask 先提交、GenerationTask 后提交」——生成域提交失败就留下
    一条指向不存在生成任务的待检项，而 qc_tasks.generation_task_id 没有物理外键。
    这条测试是回归闸门：若有人把入队挪回 process_one，它立刻红。
    """
    seen: list[tuple[str, str, str]] = []

    async def enqueue(task, outcome) -> None:
        seen.append((task.entity_id, outcome.asset_version_id, outcome.quality_profile_version))

    worker, repo, service = _setup(RecordingExecutor(), qc_enqueuer=enqueue)
    task = await _requested(service, repo)

    assert await worker.process_one() is True
    assert seen == []
    assert repo.snapshot()[task.entity_id].status is GenerationTaskStatus.RUNNING


async def test_t3_success_event_carries_product_refs_and_ownership() -> None:
    """GENERATION_SUCCEEDED 由 T3 写，且必须自带产物与归属供 QC 入队。"""
    from services.project_planning.domain.state_machine import GENERATION_PROCESS_ACTION

    repo = FakeTaskRepository()
    outbox = InMemoryOutboxWriter()
    service = GenerationService(repo, outbox)
    task = await _requested(service, repo)
    await service.claim(task_id=task.entity_id, worker_id="worker-1", lease_seconds=60)
    task = repo.snapshot()[task.entity_id]
    task.quality_profile_version = "qp-42"
    expected_version = task.entity_version
    task.transition_to(
        GenerationTaskStatus.AWAITING_ASSET_REGISTRATION,
        "t1-confirm",
        GENERATION_PROCESS_ACTION,
    )
    await repo.save(task, expected_version)  # 模拟 T1 已提交

    done = await service.complete_from_receipt(
        generation_task_id=task.generation_task_id,
        asset_version_id="av-42",
        reason="receipt-ok",
    )

    assert done.status is GenerationTaskStatus.SUCCEEDED
    succeeded = [e for e in outbox.events if e["event_type"] == "GENERATION_SUCCEEDED"]
    assert len(succeeded) == 1
    payload = succeeded[0]["payload"]
    assert payload["asset_version_id"] == "av-42"
    assert payload["quality_profile_version"] == "qp-42"
    assert payload["project_id"] == "proj-1"
    assert payload["shot_id"] == "shot-1"
    assert payload["generation_task_id"] == task.generation_task_id


async def test_t3_persists_product_refs_on_task() -> None:
    """产物引用只由回执落库；Worker 内存里的值不构成登记事实。"""
    from services.project_planning.domain.state_machine import GENERATION_PROCESS_ACTION

    _worker, repo, service = _setup(RecordingExecutor())
    task = await _requested(service, repo)
    await service.claim(task_id=task.entity_id, worker_id="worker-1", lease_seconds=60)
    task = repo.snapshot()[task.entity_id]
    task.quality_profile_version = "qp-r16-v1"
    expected_version = task.entity_version
    task.transition_to(
        GenerationTaskStatus.AWAITING_ASSET_REGISTRATION,
        "t1-confirm",
        GENERATION_PROCESS_ACTION,
    )
    await repo.save(task, expected_version)  # 模拟 T1 已提交

    done = await service.complete_from_receipt(
        generation_task_id=task.generation_task_id,
        asset_version_id="asset-ver-1",
    )
    assert repo.snapshot()[task.entity_id].asset_version_id == "asset-ver-1"
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

    from services.project_planning.domain.state_machine import GENERATION_PROCESS_ACTION

    worker, repo, service = _setup(RecordingExecutor(), qc_enqueuer=enqueue, qc_lookup=lookup)
    task = await _requested(service, repo)
    await service.claim(task_id=task.entity_id, worker_id="worker-1", lease_seconds=60)
    task = repo.snapshot()[task.entity_id]
    task.quality_profile_version = "qp-r16-v1"
    expected_version = task.entity_version
    task.transition_to(
        GenerationTaskStatus.AWAITING_ASSET_REGISTRATION,
        "t1-confirm",
        GENERATION_PROCESS_ACTION,
    )
    await repo.save(task, expected_version)  # 模拟 T1 已提交
    await service.complete_from_receipt(
        generation_task_id=task.generation_task_id, asset_version_id="asset-ver-1"
    )
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


async def test_executor_success_never_lets_worker_write_terminal_state() -> None:
    """执行器成功不带资产 ID：Worker 没有凭据写 SUCCEEDED。"""
    worker, repo, service = _setup(
        RecordingExecutor(GenerationOutcome("SUCCEEDED", "asset://out.mp4"))
    )
    task = await _requested(service, repo)

    assert await worker.process_one() is True
    done = repo.snapshot()[task.entity_id]
    assert done.status is GenerationTaskStatus.RUNNING
    assert done.asset_version_id == ""


async def test_claim_and_finish_use_separate_short_transactions() -> None:
    """P1-01 判据：领取与终结各自开作用域并提交。

    原实现全程一个事务、执行完才 commit：租约在执行期间不可见、行锁不释放，
    多 Worker 只能串行。作用域与提交计数是这条语义的可观测形式。

    用失败执行器才能让 Worker 自己写终态：成功路径的终态属于 T3，不在本用例
    的事务边界内。
    """
    worker, repo, service = _setup(RecordingExecutor(GenerationOutcome("FAILED", "boom")))
    task = await _requested(service, repo)

    assert await worker.process_one() is True

    assert repo.opened == 2  # 领取 + 终结
    assert repo.commits == ["commit", "commit"]
    assert repo.snapshot()[task.entity_id].status is GenerationTaskStatus.FAILED


async def test_lease_is_committed_before_executor_runs() -> None:
    """执行器开跑的那一刻，领取必须已经提交。

    这是「租约作为并发事实」的最小判据：若执行期间才提交，其他 Worker 读到的
    仍是 REQUESTED，看不见任务已被持有。
    """
    repo = FakeTaskRepository()
    service = GenerationService(repo, InMemoryOutboxWriter())
    commits_at_start: list[int] = []

    class ObservingExecutor:
        async def execute(self, task):
            commits_at_start.append(len(repo.commits))
            return GenerationOutcome("SUCCEEDED", "ev")

    worker = GenerationWorker(
        scope=_scope_factory(repo, service),
        executor=ObservingExecutor(),
        worker_id="worker-1",
        lease_seconds=60,
        idle_sleep=0.05,
    )
    await _requested(service, repo)

    assert await worker.process_one() is True

    assert commits_at_start == [1], "领取必须已提交，终结尚未提交"


async def test_expired_lease_cannot_renew_or_finish() -> None:
    """租约过期后，即使 owner 一致也不得续租/终结。

    回收扫描是周期性的，到期到被扫到之间有空窗；只看 owner 不看时间，
    旧 Worker 能在这个空窗里续租或落终态，产出「已失去租约的人写的结果」。
    """
    _worker, repo, service = _setup(RecordingExecutor())
    task = await _requested(service, repo)
    await service.claim(task_id=task.entity_id, worker_id="worker-1", lease_seconds=60)
    repo.snapshot()[task.entity_id].lease_expires_at = datetime.now(UTC) - timedelta(
        seconds=1
    )

    with pytest.raises(DomainError, match="过期"):
        await service.renew(
            task_id=task.entity_id, worker_id="worker-1", lease_seconds=60
        )
    with pytest.raises(DomainError, match="过期"):
        await service.finish(
            task_id=task.entity_id, worker_id="worker-1", result="FAILED"
        )
