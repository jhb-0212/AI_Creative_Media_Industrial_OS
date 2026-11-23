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


async def test_success_does_not_enqueue_qc_directly() -> None:
    """成功**不再**直接写 QC 表：入队只能由 GENERATION_SUCCEEDED 事件触发。

    直接写会出现「QCTask 先提交、GenerationTask 后提交」——生成域提交失败就留下
    一条指向不存在生成任务的待检项，而 qc_tasks.generation_task_id 没有物理外键。
    这条测试是回归闸门：若有人把入队挪回 process_one，它立刻红。
    """
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
    assert seen == []
    assert repo.snapshot()[task.entity_id].status is GenerationTaskStatus.SUCCEEDED


async def test_success_event_carries_product_refs_and_ownership() -> None:
    """GENERATION_SUCCEEDED 必须自带产物与归属——QC 域就靠这条事件入队。

    消费方拿不到产物引用就无从确定待检资产；拿不到 project_id/shot_id 就得反查，
    而反查可能读到新版本。事件是自足的事实，不是「去库里再问一次」的通知。
    """
    repo = FakeTaskRepository()
    outbox = InMemoryOutboxWriter()
    service = GenerationService(repo, outbox)
    worker = GenerationWorker(
        # P1-01 起 Worker 按阶段开短工作单元，构造时只接受作用域工厂：
        # 绑死一个 Session 会让租约与终态回到同一事务里。
        scope=_scope_factory(repo, service),
        executor=RecordingExecutor(
            GenerationOutcome(
                "SUCCEEDED",
                "asset://out.mp4",
                asset_version_id="av-42",
                quality_profile_version="qp-42",
            )
        ),
        worker_id="worker-1",
        lease_seconds=60,
        idle_sleep=0.05,
    )
    task = await _requested(service, repo)

    assert await worker.process_one() is True

    succeeded = [e for e in outbox.events if e["event_type"] == "GENERATION_SUCCEEDED"]
    assert len(succeeded) == 1
    payload = succeeded[0]["payload"]
    assert payload["asset_version_id"] == "av-42"
    assert payload["quality_profile_version"] == "qp-42"
    assert payload["project_id"] == "proj-1"
    assert payload["shot_id"] == "shot-1"
    assert payload["generation_task_id"] == task.generation_task_id


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


async def test_claim_and_finish_use_separate_short_transactions() -> None:
    """P1-01 判据：领取与终结各自开作用域并提交。

    原实现全程一个事务、执行完才 commit：租约在执行期间不可见、行锁不释放，
    多 Worker 只能串行。作用域与提交计数是这条语义的可观测形式。

    替身默认给「合法成功」（带产物引用），因此这里断言 SUCCEEDED 与 P1-02 的
    产物契约不冲突：`_setup(RecordingExecutor())` 不会触发违约分支。
    """
    worker, repo, service = _setup(RecordingExecutor())
    task = await _requested(service, repo)

    assert await worker.process_one() is True


    assert repo.opened == 2  # 领取 + 终结
    assert repo.commits == ["commit", "commit"]
    assert repo.snapshot()[task.entity_id].status is GenerationTaskStatus.SUCCEEDED


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
            task_id=task.entity_id, worker_id="worker-1", result="SUCCEEDED"
        )
