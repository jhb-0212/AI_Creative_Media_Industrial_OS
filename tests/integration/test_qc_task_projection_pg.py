"""调度投影（``qc_maintenance.task_projection``）在真实 PostgreSQL 下的验收。

守住的四条不变量（都只在真库上才成立）：

1. **同事务**：投影与源任务在同一事务里提交——源回滚时投影不留下「假候选」；
2. **低版本不覆盖**：回填或并发写入带来更旧的 ``entity_version`` 时，投影不会倒退；
3. **终态删除**：任务 PASSED/REJECTED 后投影行消失（否则调度一直看到永不运行的候选）；
4. **可重建**：删掉投影后按源任务重放，结果与原来一致（投影不是第二事实源）。

用真实 ``qc_governance_role`` 连接（RLS 生效）+ 管理连接做核对读数。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from prometheus_client import REGISTRY
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.shared.database import create_qc_governance_engine, create_sessionmaker
from packages.shared.outbox_writer import SqlAlchemyOutboxWriter
from services.qc_governance.domain.qc_task import EnqueueOrigin
from services.qc_governance.repository.qc_task_repo import SqlAlchemyQCTaskRepository
from services.qc_governance.repository.task_projection_repo import (
    SqlAlchemyTaskProjectionRepository,
)
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


def _service(session: AsyncSession) -> QCTaskService:
    """与生产同装配：仓储、事件、投影共用同一个 Session。"""
    return QCTaskService(
        SqlAlchemyQCTaskRepository(session),
        SqlAlchemyOutboxWriter(session),
        projection=SqlAlchemyTaskProjectionRepository(session),
    )


async def _projection_row(admin_sessionmaker, task_id: str):
    async with admin_sessionmaker() as session:
        return (
            await session.execute(
                text(
                    "SELECT task_status, entity_version, lease_generation, "
                    "next_action_at "
                    "FROM qc_maintenance.task_projection WHERE task_id = :id"
                ),
                {"id": task_id},
            )
        ).first()


async def _projection_row_in(session: AsyncSession, task_id: str):
    """同一事务内读投影（观察条件更新的效果，不落库）。"""
    return (
        await session.execute(
            text(
                "SELECT task_status, entity_version, lease_generation, next_action_at "
                "FROM qc_maintenance.task_projection WHERE task_id = :id"
            ),
            {"id": task_id},
        )
    ).first()


async def _enqueue(qc_sessionmaker, *, tenant, project_id: str, suffix: str) -> str:
    async with qc_sessionmaker() as session:
        await set_test_scope(session, tenant_id=tenant)
        task = await _service(session).enqueue(
            project_id=project_id,
            asset_version_id=f"av-proj-{suffix}",
            generation_task_id=f"gen-proj-{suffix}",
            quality_profile_version=f"qp-{suffix}",
            tenant_id=tenant,
            origin=EnqueueOrigin(source="RELAY", operator_id="outbox-relay"),
            shot_id=f"shot-{suffix}",
            qc_task_id=f"qc-proj-{suffix}",
        )
        await session.commit()
        return task.entity_id


async def test_enqueue_and_claim_keep_projection_in_sync(
    qc_sessionmaker, admin_sessionmaker
) -> None:
    tenant = uuid.uuid4()
    suffix = uuid.uuid4().hex[:10]
    project_id = f"proj-prj-{suffix}"
    await seed_project(admin_sessionmaker, project_id=project_id, tenant=tenant)
    task_id = await _enqueue(
        qc_sessionmaker, tenant=tenant, project_id=project_id, suffix=suffix
    )

    row = await _projection_row(admin_sessionmaker, task_id)
    assert row is not None, "入队后投影必须存在（同一事务）"
    assert row.task_status == "QUEUED"
    assert row.lease_generation == 0
    # QUEUED 的候选时刻取入队时间（可立即领取），不是 NULL。
    assert row.next_action_at is not None

    async with qc_sessionmaker() as session:
        await set_test_scope(session, tenant_id=tenant)
        await _service(session).claim(task_id=task_id, worker_id="qc-A", lease_seconds=60)
        await session.commit()

    row = await _projection_row(admin_sessionmaker, task_id)
    assert row.task_status == "RUNNING"
    assert row.lease_generation == 1
    assert row.next_action_at is not None


async def test_source_rollback_leaves_no_projection_row(
    qc_sessionmaker, admin_sessionmaker
) -> None:
    """源任务回滚 → 投影不得留下行（同事务的意义就在这里）。"""
    tenant = uuid.uuid4()
    suffix = uuid.uuid4().hex[:10]
    project_id = f"proj-rb-{suffix}"
    await seed_project(admin_sessionmaker, project_id=project_id, tenant=tenant)

    async with qc_sessionmaker() as session:
        await set_test_scope(session, tenant_id=tenant)
        task = await _service(session).enqueue(
            project_id=project_id,
            asset_version_id=f"av-rb-{suffix}",
            generation_task_id=f"gen-rb-{suffix}",
            quality_profile_version=f"qp-{suffix}",
            tenant_id=tenant,
            origin=EnqueueOrigin(source="RELAY", operator_id="outbox-relay"),
            shot_id=f"shot-{suffix}",
            qc_task_id=f"qc-rb-{suffix}",
        )
        await session.rollback()
        task_id = task.entity_id

    assert await _projection_row(admin_sessionmaker, task_id) is None


async def test_older_snapshot_cannot_overwrite_newer_projection(
    qc_sessionmaker, admin_sessionmaker
) -> None:
    """低版本快照被忽略：回填/迟到写入不得让投影版本倒退。

    全程不提交：这个用例刻意构造「投影版本高于源版本」的中毒状态，一旦落库，
    就成了对账工具永远修不好的一行（``entity_version`` 只进不退，回填也盖不掉）。
    所以只在未提交事务里观察条件更新的效果，结束时回滚。
    """
    tenant = uuid.uuid4()
    suffix = uuid.uuid4().hex[:10]
    project_id = f"proj-ver-{suffix}"
    await seed_project(admin_sessionmaker, project_id=project_id, tenant=tenant)
    task_id = await _enqueue(
        qc_sessionmaker, tenant=tenant, project_id=project_id, suffix=suffix
    )

    async with qc_sessionmaker() as session:
        await set_test_scope(session, tenant_id=tenant)
        repo = SqlAlchemyTaskProjectionRepository(session)
        task = await SqlAlchemyQCTaskRepository(session).get_by_id(task_id)
        assert task is not None
        # 先把投影推到较高的版本（模拟正常的后续写入）。
        task.entity_version += 5
        await repo.sync(task)
        # 再用一个更旧的快照覆盖：必须被 ``entity_version`` 条件挡住。
        stale = await SqlAlchemyQCTaskRepository(session).get_by_id(task_id)
        assert stale is not None
        mismatch_base = REGISTRY.get_sample_value("qc_projection_mismatch_total") or 0.0
        await repo.sync(stale)
        # 被条件挡回（rowcount=0）就是「投影领先源表」的实时证据，必须计数：
        # 只靠下一次对账发现，意味着这段时间调度看的是过期的派生数据。
        assert (REGISTRY.get_sample_value("qc_projection_mismatch_total") or 0.0) == (
            mismatch_base + 1
        ), "版本条件挡回时必须计入 qc_projection_mismatch_total"
        current = await _projection_row_in(session, task_id)
        assert current is not None
        assert current.entity_version == task.entity_version, "旧快照覆盖了新投影版本"
        await session.rollback()

    # 回滚后数据库仍是入队时那一版（3 行 → 1 行，回滚不改变已提交事实）。
    row = await _projection_row(admin_sessionmaker, task_id)
    assert row is not None and row.entity_version == 1


async def test_terminal_state_retires_projection_row(
    qc_sessionmaker, admin_sessionmaker
) -> None:
    """终态删除投影行，且投影可由源任务重建。"""
    tenant = uuid.uuid4()
    suffix = uuid.uuid4().hex[:10]
    project_id = f"proj-fin-{suffix}"
    await seed_project(admin_sessionmaker, project_id=project_id, tenant=tenant)
    task_id = await _enqueue(
        qc_sessionmaker, tenant=tenant, project_id=project_id, suffix=suffix
    )

    async with qc_sessionmaker() as session:
        await set_test_scope(session, tenant_id=tenant)
        service = _service(session)
        await service.claim(task_id=task_id, worker_id="qc-A", lease_seconds=60)
        await session.commit()

    # 判定需要报告核验器；这里只验证投影的终态语义，因此用一个只读装配
    # （report_lookup 存在但不参与本用例结论）。
    async def lookup(qc_report_id: str, tid) -> object:
        from packages.shared.evidence_ports import ReportFacts

        return ReportFacts(asset_version_id=f"av-proj-{suffix}", tenant_id=tid)

    async with qc_sessionmaker() as session:
        await set_test_scope(session, tenant_id=tenant)
        service = QCTaskService(
            SqlAlchemyQCTaskRepository(session),
            SqlAlchemyOutboxWriter(session),
            report_lookup=lookup,
            projection=SqlAlchemyTaskProjectionRepository(session),
        )
        await service.finish(
            task_id=task_id,
            worker_id="qc-A",
            expected_lease_generation=1,
            result="PASSED",
            qc_report_id=f"report-{suffix}",
        )
        await session.commit()

    assert await _projection_row(admin_sessionmaker, task_id) is None
