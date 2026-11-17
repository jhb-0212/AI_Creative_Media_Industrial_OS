"""Worker 崩溃恢复证据（契约 9.4 / 12.6）。

场景：worker 领走任务后进程死掉——不写终态、不再续租。数据库里留下
``RUNNING`` + 过期租约。恢复由另一实例在租约到期后接管完成。

为什么要按这个顺序断言：
- 只断言"最终成功了"证明不了恢复机制存在——如果实现改回"无限重试直到成功"，
  任务也会成功，但崩溃任务会永远卡住；
- 因此先断言**接管被计量**（``reclaimed > 0``），再断言**恢复耗时**在
  12.6 的 5 分钟目标内，最后断言**不重复产出**（幂等重试）。

崩溃是模拟的：领取后用 SQL 把租约推到过去。真实崩溃不需要被测代码配合，
这一点很重要——如果恢复只能由"优雅停机钩子"触发，那它救不了 kill -9。
"""

from __future__ import annotations

import asyncio
import time
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from services.generation_execution.domain.enums import GenerationTaskStatus
from services.generation_execution.repository.generation_task_sqlalchemy_repo import (
    SqlAlchemyGenerationTaskRepository,
)
from services.generation_execution.service.generation_service import (
    GenerationTaskLifecycle,
)
from tests.conftest import TEST_TENANT_ID, apply_db_context
from tests.resilience.recording import record
from tests.support.generation_seed import new_route_id, seed_task
from workers.generation.adapter import DeterministicGenerationAdapter
from workers.generation.asset_registrar import (
    AssetVersionRegistrar,
    derive_asset_version_id,
)
from workers.generation.executor import (
    GenerationExecutorContext,
    GenerationRoundStats,
    execute_round,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.resilience]

#: 崩溃任务数。
CRASHED_TASKS = 6

#: 演练用租约（远小于生产 300s，只为了让"过期"在秒级发生）。
DRILL_LEASE_SECONDS = 1.0

#: 12.6 的建议目标：租约到期后 5 分钟内重新调度。
RECOVERY_SLO_SECONDS = 300.0


def _context(project_id: str, worker_id: str) -> GenerationExecutorContext:
    return GenerationExecutorContext(
        tenant_id=TEST_TENANT_ID,
        organization_id=TEST_TENANT_ID,
        project_ids=frozenset({project_id}),
        worker_id=worker_id,
    )


async def test_crashed_worker_tasks_recovered_within_slo(
    gen_sm: async_sessionmaker[AsyncSession],
    asset_sm: async_sessionmaker[AsyncSession],
) -> None:
    """领取后崩溃 → 租约到期 → 另一 worker 接管完成，耗时在 5 分钟内。"""
    project = f"proj_crash_{uuid.uuid4().hex[:8]}"
    route_id = new_route_id()
    tasks = [
        await seed_task(
            gen_sm,
            entity_id=f"gen_crash_{uuid.uuid4().hex[:8]}",
            route_id=route_id,
            project_id=project,
            status=GenerationTaskStatus.PENDING,
        )
        for _ in range(CRASHED_TASKS)
    ]

    # 崩溃点：worker 领走任务，然后进程消失（不写终态、不续租）。
    async with gen_sm() as session:
        await apply_db_context(session, project_ids=(project,))
        repo = SqlAlchemyGenerationTaskRepository(session)
        lifecycle = GenerationTaskLifecycle(repo)
        for task in tasks:
            stored = await repo.get_by_id(task.entity_id)
            assert stored is not None
            await lifecycle.claim(
                stored,
                actor="svc_generation_executor",
                lease_owner="host-crashed:1",
                lease_seconds=DRILL_LEASE_SECONDS,
            )
        await session.commit()
    crashed_at = time.perf_counter()

    survivor = _context(project, "host-survivor:2")
    registrar = AssetVersionRegistrar(asset_sessionmaker=asset_sm)
    reclaimed = 0
    deadline = crashed_at + RECOVERY_SLO_SECONDS
    while True:
        stats = await execute_round(
            gen_sm,
            survivor,
            DeterministicGenerationAdapter(),
            GenerationRoundStats(),
            registrar=registrar,
            lease_seconds=DRILL_LEASE_SECONDS,
        )
        reclaimed += stats.reclaimed
        if reclaimed >= CRASHED_TASKS:
            break
        # 租约还没到期：真实的 worker 也是靠轮询等它过期，这里同样等。
        assert time.perf_counter() < deadline, "崩溃任务未在 SLO 内被接管"
        await asyncio.sleep(0.1)
    recovered_seconds = time.perf_counter() - crashed_at

    assert recovered_seconds < RECOVERY_SLO_SECONDS

    async with gen_sm() as session:
        await apply_db_context(session, project_ids=(project,))
        repo = SqlAlchemyGenerationTaskRepository(session)
        stored = [await repo.get_by_id(task.entity_id) for task in tasks]
    assert all(task is not None for task in stored)
    assert all(
        task.status is GenerationTaskStatus.SUCCEEDED for task in stored if task
    )
    # 恢复后归属新持有者：旧 owner 不再持有任何任务。
    assert {task.lease_owner for task in stored if task} == {"host-survivor:2"}

    async with asset_sm() as session:
        await apply_db_context(session, project_ids=(project,))
        asset_count = await session.scalar(
            text(
                "SELECT count(*) FROM asset_service.asset_versions "
                "WHERE project_id = :pid"
            ),
            {"pid": project},
        )
    # 幂等重试：6 个任务只产出 6 个资产版本（接管不产生重复产出）。
    assert int(asset_count or 0) == CRASHED_TASKS
    assert {derive_asset_version_id(task) for task in tasks} == {
        task.result_ref for task in stored if task
    }

    record(
        "worker_crash_recovery",
        crashed_tasks=CRASHED_TASKS,
        drill_lease_seconds=DRILL_LEASE_SECONDS,
        reclaimed=reclaimed,
        recovered_seconds=round(recovered_seconds, 3),
        slo_seconds=RECOVERY_SLO_SECONDS,
        within_slo=recovered_seconds < RECOVERY_SLO_SECONDS,
        asset_versions=int(asset_count or 0),
        invariant="reclaimed == crashed_tasks && asset_versions == crashed_tasks",
    )


async def test_recovered_task_is_not_revived_or_reexecuted(
    gen_sm: async_sessionmaker[AsyncSession],
    asset_sm: async_sessionmaker[AsyncSession],
) -> None:
    """恢复完成的终态任务不再被领取：既不复活，也不重复执行。"""
    project = f"proj_settled_{uuid.uuid4().hex[:8]}"
    task = await seed_task(
        gen_sm,
        entity_id=f"gen_settled_{uuid.uuid4().hex[:8]}",
        route_id=new_route_id(),
        project_id=project,
        status=GenerationTaskStatus.PENDING,
    )
    context = _context(project, "host-only:1")
    registrar = AssetVersionRegistrar(asset_sessionmaker=asset_sm)

    first = await execute_round(
        gen_sm,
        context,
        DeterministicGenerationAdapter(),
        GenerationRoundStats(),
        registrar=registrar,
        lease_seconds=DRILL_LEASE_SECONDS,
    )
    assert (first.claimed, first.succeeded) == (1, 1)

    # 再跑一轮：SUCCEEDED 无出边，不该出现在任何候选集里。
    second = await execute_round(
        gen_sm,
        context,
        DeterministicGenerationAdapter(),
        GenerationRoundStats(),
        registrar=registrar,
        lease_seconds=DRILL_LEASE_SECONDS,
    )
    assert (second.claimed, second.reclaimed, second.succeeded, second.failed) == (
        0,
        0,
        0,
        0,
    )

    async with gen_sm() as session:
        await apply_db_context(session, project_ids=(project,))
        stored = await SqlAlchemyGenerationTaskRepository(session).get_by_id(
            task.entity_id
        )
    assert stored is not None
    assert stored.status is GenerationTaskStatus.SUCCEEDED
    assert stored.result_ref == derive_asset_version_id(task)
