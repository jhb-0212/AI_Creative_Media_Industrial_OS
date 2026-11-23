"""P1-04：QC Worker 在真实 PostgreSQL 下的并发验收（与 Generation 链同构）。

覆盖审查报告 P1-04 的 QC 同构场景：双 Worker 领取互斥、SKIP LOCKED 分流、
领取后独立会话可见、执行期不持长事务、过期租约拒绝续租/判定、并发回收唯一、
回收后旧 Worker 迟到判定被拒。

只用真实组件（qc_governance_role 连接 + 真实仓储/服务/事务），同步用
``asyncio.Barrier``，不用 sleep 猜窗口；断言落在数据库状态与租约字段上。
所有样本唯一 id，不删任何 append-only 审计事实。
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
    create_qc_governance_engine,
    create_sessionmaker,
)
from packages.shared.outbox_writer import SqlAlchemyOutboxWriter
from services.project_planning.domain.enums import QCTaskStatus
from services.qc_governance.repository.qc_task_repo import SqlAlchemyQCTaskRepository
from services.qc_governance.service.qc_task_service import QCTaskService
from tests.conftest import set_test_scope
from tests.integration.planning_seed import seed_project


@pytest.fixture(scope="module")
async def qc_sessionmaker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_qc_governance_engine()
    sm = create_sessionmaker(engine)
    try:
        yield sm
    finally:
        await engine.dispose()


_qc_holder: dict[str, async_sessionmaker[AsyncSession]] = {}


def _qc_sm() -> async_sessionmaker[AsyncSession]:
    return _qc_holder["sm"]


@pytest.fixture(autouse=True)
def _bind_qc_sm(qc_sessionmaker) -> None:
    _qc_holder["sm"] = qc_sessionmaker


async def _seed_project(admin_sessionmaker, *, tenant: uuid.UUID, suffix: str) -> str:
    project_id = f"conc-qc-{suffix}"
    await seed_project(admin_sessionmaker, project_id=project_id, tenant=tenant)
    return project_id


async def _enqueue(
    *, tenant: uuid.UUID, project_id: str, suffix: str
) -> str:
    async with _qc_sm()() as session:
        await set_test_scope(session, tenant_id=tenant)
        service = QCTaskService(
            SqlAlchemyQCTaskRepository(session), SqlAlchemyOutboxWriter(session)
        )
        task = await service.enqueue(
            project_id=project_id,
            asset_version_id=f"av-{suffix}",
            generation_task_id=f"gen-{suffix}",
            quality_profile_version=f"qp-{suffix}",
            created_by="pg-concurrency-test",
            tenant_id=tenant,
            shot_id=f"shot-{suffix}",
            qc_task_id=f"qc-{suffix}",
        )
        await session.commit()
        return task.entity_id


async def _claim(task_id: str, *, tenant: uuid.UUID, worker_id: str, lease: int) -> Any:
    async with _qc_sm()() as session:
        await set_test_scope(session, tenant_id=tenant)
        repo = SqlAlchemyQCTaskRepository(session)
        candidate = await repo.next_claimable_for_claim()
        assert candidate is not None, "无可领取候选"
        service = QCTaskService(repo, SqlAlchemyOutboxWriter(session))
        claimed = await service.claim(
            task_id=candidate.entity_id, worker_id=worker_id, lease_seconds=lease
        )
        await session.commit()
        return claimed


async def _read_row(admin_sessionmaker, task_id: str) -> Any:
    async with admin_sessionmaker() as session:
        return (
            await session.execute(
                text(
                    "SELECT status, lease_owner, lease_expires_at, attempt_count "
                    "FROM qc_governance.qc_tasks WHERE entity_id = :id"
                ),
                {"id": task_id},
            )
        ).one()


async def _assert_waiting_on_lock(admin_sessionmaker, pid: int, label: str) -> None:
    deadline = time.monotonic() + 5.0
    async with admin_sessionmaker() as observer:
        while time.monotonic() < deadline:
            row = (
                await observer.execute(
                    text("SELECT wait_event_type FROM pg_stat_activity WHERE pid = :pid"),
                    {"pid": pid},
                )
            ).first()
            if row is not None and row[0] == "Lock":
                return
            await asyncio.sleep(0.02)
    raise AssertionError(f"{label}（pid={pid}）未进入 PG Lock 等待：并发证据不成立")


# --------------------------------------------------------------------------- #
# 强证据：A 持锁未提交期间，B 的领取查询必须跳过锁行；不带 SKIP LOCKED 必须等待
# --------------------------------------------------------------------------- #
async def test_skip_locked_bypasses_row_held_by_uncommitted_claim(
    admin_sessionmaker,
) -> None:
    tenant = uuid.uuid4()
    suffix = uuid.uuid4().hex[:10]
    project_id = await _seed_project(admin_sessionmaker, tenant=tenant, suffix=suffix)
    task_id = await _enqueue(tenant=tenant, project_id=project_id, suffix=suffix)

    lock_held = asyncio.Event()
    release_lock = asyncio.Event()

    async def holder() -> None:
        async with _qc_sm()() as session:
            await set_test_scope(session, tenant_id=tenant)
            repo = SqlAlchemyQCTaskRepository(session)
            candidate = await repo.next_claimable_for_claim()
            assert candidate is not None and candidate.entity_id == task_id
            lock_held.set()
            await release_lock.wait()
            await session.rollback()

    holder_task = asyncio.create_task(holder())
    await lock_held.wait()

    # 正侧：带 SKIP LOCKED 的领取查询跳过锁行，立即返回 None。
    async with _qc_sm()() as session:
        await set_test_scope(session, tenant_id=tenant)
        repo = SqlAlchemyQCTaskRepository(session)
        got = await asyncio.wait_for(repo.next_claimable_for_claim(), timeout=5.0)
        assert got is None, "SKIP LOCKED 未跳过被占用行：B 领到了 A 正在领取的 QC 任务"

    # 反侧：不带 SKIP LOCKED 的 FOR UPDATE 必须等待，证明锁窗口真实存在。
    async with _qc_sm()() as session:
        await set_test_scope(session, tenant_id=tenant)
        await session.execute(text("SET LOCAL lock_timeout = '8s'"))
        pid = int((await session.execute(text("SELECT pg_backend_pid()"))).scalar_one())
        pending = asyncio.create_task(
            session.execute(
                text(
                    "SELECT entity_id FROM qc_governance.qc_tasks "
                    "WHERE entity_id = :id FOR UPDATE"
                ),
                {"id": task_id},
            )
        )
        try:
            await _assert_waiting_on_lock(admin_sessionmaker, pid, "QC 领取竞争方 FOR UPDATE")
            assert not pending.done()
        finally:
            release_lock.set()
            await holder_task
        result = await asyncio.wait_for(pending, timeout=10.0)
        assert result.scalar_one() == task_id
        await session.rollback()

    row = await _read_row(admin_sessionmaker, task_id)
    assert row.status == QCTaskStatus.QUEUED.value


async def test_two_workers_claim_distinct_tasks_without_blocking(
    admin_sessionmaker,
) -> None:
    tenant = uuid.uuid4()
    suffix = uuid.uuid4().hex[:10]
    project_id = await _seed_project(admin_sessionmaker, tenant=tenant, suffix=suffix)
    await _enqueue(tenant=tenant, project_id=project_id, suffix=f"{suffix}-a")
    await _enqueue(tenant=tenant, project_id=project_id, suffix=f"{suffix}-b")

    barrier = asyncio.Barrier(2)
    claimed: dict[str, str] = {}

    async def worker(name: str) -> None:
        async with _qc_sm()() as session:
            await set_test_scope(session, tenant_id=tenant)
            repo = SqlAlchemyQCTaskRepository(session)
            await barrier.wait()
            candidate = await repo.next_claimable_for_claim()
            assert candidate is not None
            service = QCTaskService(repo, SqlAlchemyOutboxWriter(session))
            await service.claim(
                task_id=candidate.entity_id, worker_id=name, lease_seconds=60
            )
            await session.commit()
            claimed[name] = candidate.entity_id

    await asyncio.gather(worker("qc-A"), worker("qc-B"))
    assert set(claimed) == {"qc-A", "qc-B"}
    assert claimed["qc-A"] != claimed["qc-B"], "两个 QC Worker 领到同一任务"
    for task_id, owner in ((claimed["qc-A"], "qc-A"), (claimed["qc-B"], "qc-B")):
        row = await _read_row(admin_sessionmaker, task_id)
        assert row.status == QCTaskStatus.RUNNING.value
        assert row.lease_owner == owner
        assert row.lease_expires_at > datetime.now(UTC)


async def test_same_task_claimed_by_only_one_worker(admin_sessionmaker) -> None:
    tenant = uuid.uuid4()
    suffix = uuid.uuid4().hex[:10]
    project_id = await _seed_project(admin_sessionmaker, tenant=tenant, suffix=suffix)
    task_id = await _enqueue(tenant=tenant, project_id=project_id, suffix=suffix)

    results: dict[str, str | None] = {}
    barrier = asyncio.Barrier(2)

    async def worker(name: str) -> None:
        async with _qc_sm()() as session:
            await set_test_scope(session, tenant_id=tenant)
            repo = SqlAlchemyQCTaskRepository(session)
            await barrier.wait()
            candidate = await repo.next_claimable_for_claim()
            if candidate is None:
                results[name] = None
                return
            service = QCTaskService(repo, SqlAlchemyOutboxWriter(session))
            await service.claim(task_id=candidate.entity_id, worker_id=name, lease_seconds=60)
            await session.commit()
            results[name] = candidate.entity_id

    await asyncio.gather(worker("qc-A"), worker("qc-B"))
    winners = [n for n, tid in results.items() if tid == task_id]
    assert len(winners) == 1, f"同一 QC 任务被多方领取：{results}"
    row = await _read_row(admin_sessionmaker, task_id)
    assert row.lease_owner == winners[0]
    assert row.attempt_count == 1


async def test_claim_visible_to_independent_session(admin_sessionmaker) -> None:
    tenant = uuid.uuid4()
    suffix = uuid.uuid4().hex[:10]
    project_id = await _seed_project(admin_sessionmaker, tenant=tenant, suffix=suffix)
    task_id = await _enqueue(tenant=tenant, project_id=project_id, suffix=suffix)
    await _claim(task_id, tenant=tenant, worker_id="qc-A", lease=60)

    async with _qc_sm()() as observer:
        await set_test_scope(observer, tenant_id=tenant)
        observed = await SqlAlchemyQCTaskRepository(observer).get_by_id(task_id)
    assert observed is not None
    assert observed.status is QCTaskStatus.RUNNING
    assert observed.lease_owner == "qc-A"


async def test_no_long_transaction_after_claim(admin_sessionmaker) -> None:
    tenant = uuid.uuid4()
    suffix = uuid.uuid4().hex[:10]
    project_id = await _seed_project(admin_sessionmaker, tenant=tenant, suffix=suffix)
    task_id = await _enqueue(tenant=tenant, project_id=project_id, suffix=suffix)
    await _claim(task_id, tenant=tenant, worker_id="qc-A", lease=60)

    async with _qc_sm()() as session:
        await set_test_scope(session, tenant_id=tenant)
        await session.execute(text("SET LOCAL lock_timeout = '8s'"))
        locked = await asyncio.wait_for(
            session.execute(
                text(
                    "SELECT entity_id FROM qc_governance.qc_tasks "
                    "WHERE entity_id = :id FOR UPDATE NOWAIT"
                ),
                {"id": task_id},
            ),
            timeout=10.0,
        )
        assert locked.scalar_one() == task_id
        await session.rollback()


async def test_expired_lease_blocks_old_worker_renew_and_finish(
    admin_sessionmaker,
) -> None:
    tenant = uuid.uuid4()
    suffix = uuid.uuid4().hex[:10]
    project_id = await _seed_project(admin_sessionmaker, tenant=tenant, suffix=suffix)
    task_id = await _enqueue(tenant=tenant, project_id=project_id, suffix=suffix)
    await _claim(task_id, tenant=tenant, worker_id="qc-A", lease=60)

    async with admin_sessionmaker() as session:
        await session.execute(
            text(
                "UPDATE qc_governance.qc_tasks "
                "SET lease_expires_at = :past WHERE entity_id = :id"
            ),
            {"past": datetime.now(UTC) - timedelta(seconds=5), "id": task_id},
        )
        await session.commit()

    async with _qc_sm()() as session:
        await set_test_scope(session, tenant_id=tenant)
        service = QCTaskService(
            SqlAlchemyQCTaskRepository(session), SqlAlchemyOutboxWriter(session)
        )
        with pytest.raises(DomainError):
            await service.renew(task_id=task_id, worker_id="qc-A", lease_seconds=60)
        await session.rollback()

    async with _qc_sm()() as session:
        await set_test_scope(session, tenant_id=tenant)
        service = QCTaskService(
            SqlAlchemyQCTaskRepository(session), SqlAlchemyOutboxWriter(session)
        )
        # 未装配 report_lookup 也会 fail-closed，但过期判据在前：这里只确认被拒。
        with pytest.raises(DomainError):
            await service.finish(
                task_id=task_id,
                worker_id="qc-A",
                result="PASSED",
                qc_report_id="report-late",
            )
        await session.rollback()

    row = await _read_row(admin_sessionmaker, task_id)
    assert row.status == QCTaskStatus.RUNNING.value
    assert row.lease_owner == "qc-A"


async def test_concurrent_reclaim_only_one_succeeds(admin_sessionmaker) -> None:
    tenant = uuid.uuid4()
    suffix = uuid.uuid4().hex[:10]
    project_id = await _seed_project(admin_sessionmaker, tenant=tenant, suffix=suffix)
    task_id = await _enqueue(tenant=tenant, project_id=project_id, suffix=suffix)
    await _claim(task_id, tenant=tenant, worker_id="qc-A", lease=60)

    async with admin_sessionmaker() as session:
        await session.execute(
            text(
                "UPDATE qc_governance.qc_tasks "
                "SET lease_expires_at = :past WHERE entity_id = :id"
            ),
            {"past": datetime.now(UTC) - timedelta(seconds=5), "id": task_id},
        )
        await session.commit()

    outcomes: dict[str, str] = {}
    barrier = asyncio.Barrier(2)

    async def reaper(name: str) -> None:
        async with _qc_sm()() as session:
            await set_test_scope(session, tenant_id=tenant)
            service = QCTaskService(
                SqlAlchemyQCTaskRepository(session), SqlAlchemyOutboxWriter(session)
            )
            await barrier.wait()
            try:
                await service.release_expired(task_id=task_id)
                await session.commit()
                outcomes[name] = "ok"
            except DomainError:
                await session.rollback()
                outcomes[name] = "conflict"

    await asyncio.gather(reaper("sweeper-1"), reaper("sweeper-2"))
    assert len([r for r in outcomes.values() if r == "ok"]) == 1, f"回收未唯一：{outcomes}"

    row = await _read_row(admin_sessionmaker, task_id)
    assert row.status == QCTaskStatus.QUEUED.value
    assert row.lease_owner == ""
    assert row.lease_expires_at is None

    async with admin_sessionmaker() as session:
        cnt = (
            await session.execute(
                text(
                    "SELECT count(*) FROM outbox.outbox_events "
                    "WHERE aggregate_id = :id AND event_type = 'QC_TASK_LEASE_EXPIRED'"
                ),
                {"id": task_id},
            )
        ).scalar_one()
    assert cnt == 1, f"并发回收写出 {cnt} 条到期事件，应为 1"


async def test_late_finish_after_reclaim_rejected(admin_sessionmaker) -> None:
    tenant = uuid.uuid4()
    suffix = uuid.uuid4().hex[:10]
    project_id = await _seed_project(admin_sessionmaker, tenant=tenant, suffix=suffix)
    task_id = await _enqueue(tenant=tenant, project_id=project_id, suffix=suffix)
    await _claim(task_id, tenant=tenant, worker_id="qc-A", lease=60)

    async with admin_sessionmaker() as session:
        await session.execute(
            text(
                "UPDATE qc_governance.qc_tasks "
                "SET lease_expires_at = :past WHERE entity_id = :id"
            ),
            {"past": datetime.now(UTC) - timedelta(seconds=5), "id": task_id},
        )
        await session.commit()
    async with _qc_sm()() as session:
        await set_test_scope(session, tenant_id=tenant)
        service = QCTaskService(
            SqlAlchemyQCTaskRepository(session), SqlAlchemyOutboxWriter(session)
        )
        await service.release_expired(task_id=task_id)
        await session.commit()
    await _claim(task_id, tenant=tenant, worker_id="qc-B", lease=60)

    async with _qc_sm()() as session:
        await set_test_scope(session, tenant_id=tenant)
        service = QCTaskService(
            SqlAlchemyQCTaskRepository(session), SqlAlchemyOutboxWriter(session)
        )
        with pytest.raises(DomainError):
            await service.finish(
                task_id=task_id,
                worker_id="qc-A",
                result="PASSED",
                qc_report_id="report-late",
            )
        await session.rollback()

    row = await _read_row(admin_sessionmaker, task_id)
    assert row.status == QCTaskStatus.RUNNING.value
    assert row.lease_owner == "qc-B", "迟到的 QC 判定改写了当前持有者的租约"
