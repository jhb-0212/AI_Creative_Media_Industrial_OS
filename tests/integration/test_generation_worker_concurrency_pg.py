"""P1-04：Generation Worker 在真实 PostgreSQL 下的并发验收。

为什么必须有这一层
------------------------------------------------------------
PR #44 把 Worker 事务边界改成了「领取 → 立即提交 → 执行不持事务 →
心跳/终结各自提交」，单测验证了提交顺序，但**没有真实 PG 并发证据**：
SKIP LOCKED 的 SQL 写对，不等于多 Worker 真的能互斥领取、租约真的能被
独立会话看见、回收真的能对同一任务只生效一次。

本模块只用真实组件：真实域角色连接（production_planning_role）、真实仓储、
真实服务、真实数据库事务。同步用 ``asyncio.Barrier`` / ``Event`` 协调确定性
时点，**不使用 sleep 猜竞争窗口**；锁等待证据从 ``pg_stat_activity`` 实读。

每条断言都落在数据库事实（状态、租约字段、审计事实）上，而非仅仅是 API
返回值或内存对象。所有样本用唯一 id，不删除任何 append-only 审计历史。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.errors.error_codes import DomainError
from packages.shared.database import (
    create_production_planning_engine,
    create_sessionmaker,
)
from packages.shared.outbox_writer import SqlAlchemyOutboxWriter
from services.production_planning.repository.sqlalchemy_repos import (
    SqlAlchemyGenerationTaskRepository,
)
from services.production_planning.service.generation_service import GenerationService
from services.project_planning.domain.enums import GenerationTaskStatus
from tests.conftest import set_test_scope
from tests.integration.planning_seed import seed_project

# 等锁上限：真卡住时报超时，不让测试挂死。
_LOCK_TIMEOUT = "8s"
# 轮询 pg_stat_activity 的上限（不是「睡一会儿看协程完没完」）。
_LOCK_WAIT_TIMEOUT = 5.0
_LOCK_WAIT_POLL = 0.02


# --------------------------------------------------------------------------- #
# 装置
# --------------------------------------------------------------------------- #
async def _seed_project(admin_sessionmaker, *, tenant: uuid.UUID, suffix: str) -> str:
    project_id = f"conc-gen-{suffix}"
    await seed_project(admin_sessionmaker, project_id=project_id, tenant=tenant)
    return project_id


@pytest.fixture(scope="module")
async def prod_sessionmaker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """独立的生产域连接池（production_planning_role 真实连接）。

    自建而非复用 app.state：并发测试要的是与 admin 观察连接相互独立的一批连接，
    且不依赖 db_sessionmaker fixture 的装配顺序。
    """
    engine = create_production_planning_engine()
    sm = create_sessionmaker(engine)
    try:
        yield sm
    finally:
        await engine.dispose()


# 每个测试通过闭包拿到本模块的会话工厂；由 fixture 在测试开始时注入。
_prod_sm_holder: dict[str, async_sessionmaker[AsyncSession]] = {}


def _prod_sm() -> async_sessionmaker[AsyncSession]:
    return _prod_sm_holder["sm"]


@pytest.fixture(autouse=True)
def _bind_prod_sm(prod_sessionmaker) -> None:
    _prod_sm_holder["sm"] = prod_sessionmaker


async def _create_task(
    *,
    tenant: uuid.UUID,
    project_id: str,
    suffix: str,
) -> str:
    """用真实域角色创建一个 REQUESTED 任务，返回 entity_id。"""
    async with _prod_sm()() as session:
        await set_test_scope(session, tenant_id=tenant)
        service = GenerationService(
            SqlAlchemyGenerationTaskRepository(session),
            SqlAlchemyOutboxWriter(session),
        )
        task = await service.request_generation(
            project_id=project_id,
            shot_id=f"shot-{suffix}",
            shot_revision_id=f"rev-{suffix}",
            production_route_ref=f"route-{suffix}",
            model_adapter_ref=f"adapter-{suffix}",
            constraint_snapshot_id=f"snap-{suffix}",
            budget_ref=f"budget-{suffix}",
            prompt_ref=f"inline:{suffix}",
            created_by="pg-concurrency-test",
            tenant_id=tenant,
            generation_task_id=f"gen-{suffix}",
        )
        await session.commit()
        return task.entity_id


async def _claim(task_id: str, *, tenant: uuid.UUID, worker_id: str, lease: int) -> Any:
    """一次完整领取（短事务）：SKIP LOCKED 候选 → claim → 立即提交。"""
    async with _prod_sm()() as session:
        await set_test_scope(session, tenant_id=tenant)
        repo = SqlAlchemyGenerationTaskRepository(session)
        candidate = await repo.next_claimable_for_claim()
        assert candidate is not None, "无可领取候选"
        service = GenerationService(repo, SqlAlchemyOutboxWriter(session))
        claimed = await service.claim(
            task_id=candidate.entity_id, worker_id=worker_id, lease_seconds=lease
        )
        await session.commit()
        return claimed


async def _backend_pid(session: AsyncSession) -> int:
    return int((await session.execute(text("SELECT pg_backend_pid()"))).scalar_one())


async def _assert_waiting_on_lock(
    admin_sessionmaker, pid: int, label: str
) -> None:
    """从 pg_stat_activity 实读目标后端确实进入 Lock 等待。"""
    deadline = time.monotonic() + _LOCK_WAIT_TIMEOUT
    async with admin_sessionmaker() as observer:
        while time.monotonic() < deadline:
            row = (
                await observer.execute(
                    text(
                        "SELECT wait_event_type FROM pg_stat_activity WHERE pid = :pid"
                    ),
                    {"pid": pid},
                )
            ).first()
            if row is not None and row[0] == "Lock":
                return
            await asyncio.sleep(_LOCK_WAIT_POLL)
    raise AssertionError(f"{label}（pid={pid}）未进入 PG Lock 等待：并发证据不成立")


async def _read_row(admin_sessionmaker, task_id: str) -> Any:
    async with admin_sessionmaker() as session:
        return (
            await session.execute(
                text(
                    "SELECT status, lease_owner, lease_expires_at, attempt_count, "
                    "asset_version_id FROM production_planning.generation_tasks "
                    "WHERE entity_id = :id"
                ),
                {"id": task_id},
            )
        ).one()


# --------------------------------------------------------------------------- #
# 场景 2-b（强证据）：A 持锁未提交期间，B 的领取查询必须跳过锁行而不是等锁
# --------------------------------------------------------------------------- #
async def test_skip_locked_bypasses_row_held_by_uncommitted_claim(
    admin_sessionmaker,
) -> None:
    """真正锁住唯一候选行，验证 SKIP LOCKED 的“跳过”而非“串行碰巧”。

    两侧各证伪一次：
    - 正侧：B 的 next_claimable_for_claim（带 SKIP LOCKED）必须立即返回 None；
    - 反侧：一条不带 SKIP LOCKED 的 FOR UPDATE 查同一行必须进入 Lock 等待
            （证明 A 的锁真实存在，且观察装置确实能看到锁等待）。
    反侧若不进等待，说明“锁窗口”根本没造出来，正侧通过毫无意义。
    """
    tenant = uuid.uuid4()
    suffix = uuid.uuid4().hex[:10]
    project_id = await _seed_project(admin_sessionmaker, tenant=tenant, suffix=suffix)
    task_id = await _create_task(tenant=tenant, project_id=project_id, suffix=suffix)

    lock_held = asyncio.Event()
    release_lock = asyncio.Event()

    async def holder() -> None:
        async with _prod_sm()() as session:
            await set_test_scope(session, tenant_id=tenant)
            repo = SqlAlchemyGenerationTaskRepository(session)
            candidate = await repo.next_claimable_for_claim()  # 持 FOR UPDATE 锁
            assert candidate is not None and candidate.entity_id == task_id
            lock_held.set()  # 告诉 B：锁已持有
            await release_lock.wait()  # 不提交，保持锁窗口直到 B 验证完毕
            await session.rollback()

    holder_task = asyncio.create_task(holder())
    await lock_held.wait()

    # 正侧：B 的领取查询带 SKIP LOCKED → 跳过被锁的唯一行，立即返回 None。
    async with _prod_sm()() as session:
        await set_test_scope(session, tenant_id=tenant)
        repo = SqlAlchemyGenerationTaskRepository(session)
        got = await asyncio.wait_for(repo.next_claimable_for_claim(), timeout=5.0)
        assert got is None, "SKIP LOCKED 未跳过被占用行：B 领到了 A 正在领取的任务"

    # 反侧：不带 SKIP LOCKED 的 FOR UPDATE 必须在 A 的锁上等待。
    # 捕获等待证据后释放 A 的锁，让该查询自然完成（而不是 cancel 掉等锁协程，
    # cancel 会把 SQLAlchemy 连接留在不确定状态，退出作用域时 rollback 报错）。
    async with _prod_sm()() as session:
        await set_test_scope(session, tenant_id=tenant)
        await session.execute(text("SET LOCAL lock_timeout = '8s'"))
        blocked_pid = await _backend_pid(session)
        pending = asyncio.create_task(
            session.execute(
                text(
                    "SELECT entity_id FROM production_planning.generation_tasks "
                    "WHERE entity_id = :id FOR UPDATE"
                ),
                {"id": task_id},
            )
        )
        try:
            await _assert_waiting_on_lock(
                admin_sessionmaker, blocked_pid, "领取竞争方的 FOR UPDATE"
            )
            assert not pending.done(), "已进入锁等待却仍完成：观察点失效"
        finally:
            release_lock.set()  # 让 holder 回滚、释放行锁
            await holder_task
        result = await asyncio.wait_for(pending, timeout=10.0)  # 锁释放后完成
        assert result.scalar_one() == task_id
        await session.rollback()

    # A 回滚后任务仍是 REQUESTED（锁窗口里没有产生任何领取事实）。
    row = await _read_row(admin_sessionmaker, task_id)
    assert row.status == GenerationTaskStatus.REQUESTED.value


# --------------------------------------------------------------------------- #
# 场景 1：双 Worker 同时领取不同任务 —— SKIP LOCKED 让二者分流，不互相阻塞
# --------------------------------------------------------------------------- #
async def test_two_workers_claim_distinct_tasks_without_blocking(
    admin_sessionmaker,
) -> None:
    tenant = uuid.uuid4()
    suffix = uuid.uuid4().hex[:10]
    project_id = await _seed_project(admin_sessionmaker, tenant=tenant, suffix=suffix)
    await _create_task(tenant=tenant, project_id=project_id, suffix=f"{suffix}-a")
    await _create_task(tenant=tenant, project_id=project_id, suffix=f"{suffix}-b")

    barrier = asyncio.Barrier(2)
    claimed: dict[str, str] = {}

    async def worker(name: str) -> None:
        async with _prod_sm()() as session:
            await set_test_scope(session, tenant_id=tenant)
            repo = SqlAlchemyGenerationTaskRepository(session)
            # 两个 Worker 在拿候选前对齐，制造真实并发窗口。
            await barrier.wait()
            candidate = await repo.next_claimable_for_claim()
            assert candidate is not None
            service = GenerationService(repo, SqlAlchemyOutboxWriter(session))
            await service.claim(
                task_id=candidate.entity_id, worker_id=name, lease_seconds=60
            )
            await session.commit()
            claimed[name] = candidate.entity_id

    await asyncio.gather(worker("worker-A"), worker("worker-B"))

    assert set(claimed) == {"worker-A", "worker-B"}
    assert claimed["worker-A"] != claimed["worker-B"], (
        "两个 Worker 领到同一任务：SKIP LOCKED 未生效"
    )
    for task_id, expected_owner in (
        (claimed["worker-A"], "worker-A"),
        (claimed["worker-B"], "worker-B"),
    ):
        row = await _read_row(admin_sessionmaker, task_id)
        assert row.status == GenerationTaskStatus.RUNNING.value
        assert row.lease_owner == expected_owner
        assert row.lease_expires_at > datetime.now(UTC)


# --------------------------------------------------------------------------- #
# 场景 2：同一任务不会被两个 Worker 同时领取（互斥）
# --------------------------------------------------------------------------- #
async def test_same_task_claimed_by_only_one_worker(admin_sessionmaker) -> None:
    tenant = uuid.uuid4()
    suffix = uuid.uuid4().hex[:10]
    project_id = await _seed_project(admin_sessionmaker, tenant=tenant, suffix=suffix)
    task_id = await _create_task(tenant=tenant, project_id=project_id, suffix=suffix)

    # 只造一个任务：两个 Worker 同时抢，必有一个拿到、一个拿到 None（SKIP LOCKED
    # 跳过被锁行）。这是「同一任务不会被两方同时领取」的直接证据。
    results: dict[str, str | None] = {}
    barrier = asyncio.Barrier(2)

    async def worker(name: str) -> None:
        async with _prod_sm()() as session:
            await set_test_scope(session, tenant_id=tenant)
            repo = SqlAlchemyGenerationTaskRepository(session)
            await barrier.wait()
            candidate = await repo.next_claimable_for_claim()
            if candidate is None:
                results[name] = None
                return
            service = GenerationService(repo, SqlAlchemyOutboxWriter(session))
            # claim 前让另一 Worker 有机会先提交，触发乐观锁冲突路径。
            await service.claim(task_id=candidate.entity_id, worker_id=name, lease_seconds=60)
            await session.commit()
            results[name] = candidate.entity_id

    await asyncio.gather(worker("worker-A"), worker("worker-B"))
    winners = [name for name, tid in results.items() if tid == task_id]
    assert len(winners) == 1, f"同一任务被多个 Worker 领取：{results}"

    row = await _read_row(admin_sessionmaker, task_id)
    assert row.status == GenerationTaskStatus.RUNNING.value
    assert row.lease_owner == winners[0]
    assert row.attempt_count == 1


# --------------------------------------------------------------------------- #
# 场景 3：领取后独立 Session 立即可见 RUNNING + 有效租约
# --------------------------------------------------------------------------- #
async def test_claim_visible_to_independent_session(admin_sessionmaker) -> None:
    tenant = uuid.uuid4()
    suffix = uuid.uuid4().hex[:10]
    project_id = await _seed_project(admin_sessionmaker, tenant=tenant, suffix=suffix)
    task_id = await _create_task(tenant=tenant, project_id=project_id, suffix=suffix)

    await _claim(task_id, tenant=tenant, worker_id="worker-A", lease=60)

    # 另一条完全独立的连接读：租约必须已是可见事实，而不是只在领取者事务里。
    async with _prod_sm()() as observer:
        await set_test_scope(observer, tenant_id=tenant)
        observed = await SqlAlchemyGenerationTaskRepository(observer).get_by_id(task_id)
    assert observed is not None
    assert observed.status is GenerationTaskStatus.RUNNING
    assert observed.lease_owner == "worker-A"
    assert observed.lease_expires_at is not None


# --------------------------------------------------------------------------- #
# 场景 4：执行期间不持有长事务 / 行锁（领取提交后行可被独立会话立即加锁）
# --------------------------------------------------------------------------- #
async def test_no_long_transaction_after_claim(admin_sessionmaker) -> None:
    tenant = uuid.uuid4()
    suffix = uuid.uuid4().hex[:10]
    project_id = await _seed_project(admin_sessionmaker, tenant=tenant, suffix=suffix)
    task_id = await _create_task(tenant=tenant, project_id=project_id, suffix=suffix)
    await _claim(task_id, tenant=tenant, worker_id="worker-A", lease=60)

    # 独立会话对同一行 SELECT FOR UPDATE NOWAIT：若执行期仍持锁会立即 55P03。
    # 能在 8s 内拿到行锁 → 领取者已提交、未把锁拖到「执行阶段」。
    async with _prod_sm()() as session:
        await set_test_scope(session, tenant_id=tenant)
        await session.execute(text(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'"))
        locked = await asyncio.wait_for(
            session.execute(
                text(
                    "SELECT entity_id FROM production_planning.generation_tasks "
                    "WHERE entity_id = :id FOR UPDATE NOWAIT"
                ),
                {"id": task_id},
            ),
            timeout=10.0,
        )
        assert locked.scalar_one() == task_id
        await session.rollback()


# --------------------------------------------------------------------------- #
# 场景 5：过期租约的旧 Worker 不能续租、也不能终结
# --------------------------------------------------------------------------- #
async def test_expired_lease_blocks_old_worker_renew_and_finish(
    admin_sessionmaker,
) -> None:
    tenant = uuid.uuid4()
    suffix = uuid.uuid4().hex[:10]
    project_id = await _seed_project(admin_sessionmaker, tenant=tenant, suffix=suffix)
    task_id = await _create_task(tenant=tenant, project_id=project_id, suffix=suffix)
    await _claim(task_id, tenant=tenant, worker_id="worker-A", lease=60)

    # 用 admin 连接把租约时间推到过去（模拟沉默 expiry，不用 sleep 等真实时间）。
    async with admin_sessionmaker() as session:
        await session.execute(
            text(
                "UPDATE production_planning.generation_tasks "
                "SET lease_expires_at = :past WHERE entity_id = :id"
            ),
            {"past": datetime.now(UTC) - timedelta(seconds=5), "id": task_id},
        )
        await session.commit()

    async with _prod_sm()() as session:
        await set_test_scope(session, tenant_id=tenant)
        service = GenerationService(
            SqlAlchemyGenerationTaskRepository(session),
            SqlAlchemyOutboxWriter(session),
        )
        with pytest.raises(DomainError):
            await service.renew(
                task_id=task_id, worker_id="worker-A", lease_seconds=60
            )
        await session.rollback()

    async with _prod_sm()() as session:
        await set_test_scope(session, tenant_id=tenant)
        service = GenerationService(
            SqlAlchemyGenerationTaskRepository(session),
            SqlAlchemyOutboxWriter(session),
        )
        with pytest.raises(DomainError):
            await service.finish(
                task_id=task_id,
                worker_id="worker-A",
                result="FAILED",
                evidence_ref="late",
            )
        await session.rollback()

    row = await _read_row(admin_sessionmaker, task_id)
    assert row.status == GenerationTaskStatus.RUNNING.value, (
        "过期 Worker 仍能推进终态：回收前的空窗没有守住"
    )
    assert row.asset_version_id == "", "过期 Worker 写入了产物引用"


# --------------------------------------------------------------------------- #
# 场景 6：并发回收 —— 同一过期任务只能形成一次有效回收
# --------------------------------------------------------------------------- #
async def test_concurrent_reclaim_only_one_succeeds(admin_sessionmaker) -> None:
    tenant = uuid.uuid4()
    suffix = uuid.uuid4().hex[:10]
    project_id = await _seed_project(admin_sessionmaker, tenant=tenant, suffix=suffix)
    task_id = await _create_task(tenant=tenant, project_id=project_id, suffix=suffix)
    await _claim(task_id, tenant=tenant, worker_id="worker-A", lease=60)

    async with admin_sessionmaker() as session:
        await session.execute(
            text(
                "UPDATE production_planning.generation_tasks "
                "SET lease_expires_at = :past WHERE entity_id = :id"
            ),
            {"past": datetime.now(UTC) - timedelta(seconds=5), "id": task_id},
        )
        await session.commit()

    # 两个扫尾进程同时回收同一任务：乐观锁保证只有一个成功，另一个冲突。
    outcomes: dict[str, str] = {}
    barrier = asyncio.Barrier(2)

    async def reaper(name: str) -> None:
        async with _prod_sm()() as session:
            await set_test_scope(session, tenant_id=tenant)
            service = GenerationService(
                SqlAlchemyGenerationTaskRepository(session),
                SqlAlchemyOutboxWriter(session),
            )
            await barrier.wait()
            try:
                await service.release_expired(task_id=task_id)
                await session.commit()
                outcomes[name] = "ok"
            except DomainError as exc:
                await session.rollback()
                outcomes[name] = f"conflict:{type(exc).__name__}"

    await asyncio.gather(reaper("sweeper-1"), reaper("sweeper-2"))
    successes = [n for n, r in outcomes.items() if r == "ok"]
    assert len(successes) == 1, f"回收未唯一：{outcomes}"

    row = await _read_row(admin_sessionmaker, task_id)
    assert row.status == GenerationTaskStatus.REQUESTED.value
    assert row.lease_owner == ""
    assert row.lease_expires_at is None

    # 审计事实：该任务恰好一条 GENERATION_LEASE_EXPIRED（不是两条）。
    async with admin_sessionmaker() as session:
        cnt = (
            await session.execute(
                text(
                    "SELECT count(*) FROM outbox.outbox_events "
                    "WHERE aggregate_id = :id AND event_type = 'GENERATION_LEASE_EXPIRED'"
                ),
                {"id": task_id},
            )
        ).scalar_one()
    assert cnt == 1, f"并发回收写出 {cnt} 条到期事件，应为 1"


# --------------------------------------------------------------------------- #
# 场景 7：回收后旧 Worker 迟到的 Finish 必须被拒（租约已易主/失效）
# --------------------------------------------------------------------------- #
async def test_late_finish_after_reclaim_rejected(admin_sessionmaker) -> None:
    tenant = uuid.uuid4()
    suffix = uuid.uuid4().hex[:10]
    project_id = await _seed_project(admin_sessionmaker, tenant=tenant, suffix=suffix)
    task_id = await _create_task(tenant=tenant, project_id=project_id, suffix=suffix)
    await _claim(task_id, tenant=tenant, worker_id="worker-A", lease=60)

    # 过期 → 回收 → 新 Worker 领取（租约易主）。
    async with admin_sessionmaker() as session:
        await session.execute(
            text(
                "UPDATE production_planning.generation_tasks "
                "SET lease_expires_at = :past WHERE entity_id = :id"
            ),
            {"past": datetime.now(UTC) - timedelta(seconds=5), "id": task_id},
        )
        await session.commit()
    async with _prod_sm()() as session:
        await set_test_scope(session, tenant_id=tenant)
        service = GenerationService(
            SqlAlchemyGenerationTaskRepository(session),
            SqlAlchemyOutboxWriter(session),
        )
        await service.release_expired(task_id=task_id)
        await session.commit()
    await _claim(task_id, tenant=tenant, worker_id="worker-B", lease=60)

    # 旧 Worker（worker-A）迟到终结：既非当前 owner，也不能写终态。
    async with _prod_sm()() as session:
        await set_test_scope(session, tenant_id=tenant)
        service = GenerationService(
            SqlAlchemyGenerationTaskRepository(session),
            SqlAlchemyOutboxWriter(session),
        )
        with pytest.raises(DomainError):
            await service.finish(
                task_id=task_id,
                worker_id="worker-A",
                result="FAILED",
                evidence_ref="late-A",
            )
        await session.rollback()

    row = await _read_row(admin_sessionmaker, task_id)
    assert row.status == GenerationTaskStatus.RUNNING.value
    assert row.lease_owner == "worker-B", "迟到 Finish 改写了当前持有者的租约"
    assert row.asset_version_id == ""
