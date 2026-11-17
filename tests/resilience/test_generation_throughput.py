"""并发压测：受理侧延迟 + 执行侧 exactly-once。

两条独立的不变量：
1. **受理**在高并发下不串号：每条请求各自落一个 PENDING 任务、各自发一条
   GENERATION_REQUESTED，引用解析不因并发而错配；
2. **执行**在多 worker 并发下 exactly-once：每个任务只有一个实例真正执行，
   每次执行只产出一个资产版本——重复执行会产生重复资产与重复计费，
   这是"高并发"最容易悄悄坏掉的地方。

断言的是**不变量**（数量、唯一性、终态），记录的是**指标**（吞吐、p95）。
只断言指标会让测试随机器性能飘；只记指标不断言则等于没测。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from services.generation_execution.domain.enums import GenerationTaskStatus
from services.generation_execution.repository.generation_task_sqlalchemy_repo import (
    SqlAlchemyGenerationTaskRepository,
)
from tests.conftest import TEST_TENANT_ID, apply_db_context, make_test_headers
from tests.resilience.recording import percentile, record
from tests.support.generation_seed import (
    new_route_id,
    request_body,
    seed_route,
    seed_shot_refs,
    seed_task,
)
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

#: 执行侧规模：24 个任务 / 6 个并发 worker（单轮批量上限 16，必然发生争抢）。
TASK_COUNT = 24
WORKER_COUNT = 6

#: 受理侧规模：20 个并发请求。
ACCEPT_CONCURRENCY = 20

_LEASE_SECONDS = 300.0


def _new_project(prefix: str) -> str:
    return f"proj_{prefix}_{uuid.uuid4().hex[:8]}"


def _context(project_id: str, worker_id: str) -> GenerationExecutorContext:
    return GenerationExecutorContext(
        tenant_id=TEST_TENANT_ID,
        organization_id=TEST_TENANT_ID,
        project_ids=frozenset({project_id}),
        worker_id=worker_id,
    )


async def _asset_version_count(
    asset_sm: async_sessionmaker[AsyncSession], *, project_id: str
) -> int:
    async with asset_sm() as session:
        await apply_db_context(session, project_ids=(project_id,))
        count = await session.scalar(
            text(
                "SELECT count(*) FROM asset_service.asset_versions "
                "WHERE project_id = :pid"
            ),
            {"pid": project_id},
        )
    return int(count or 0)


async def _open_task_count(
    gen_sm: async_sessionmaker[AsyncSession], *, project_id: str
) -> int:
    """项目内未终态任务数（PENDING + RUNNING）。"""
    async with gen_sm() as session:
        await apply_db_context(session, project_ids=(project_id,))
        count = await session.scalar(
            text(
                "SELECT count(*) FROM generation_execution.generation_tasks "
                "WHERE project_id = :pid AND status IN ('PENDING', 'RUNNING')"
            ),
            {"pid": project_id},
        )
    return int(count or 0)


async def test_concurrent_workers_execute_each_task_exactly_once(
    gen_sm: async_sessionmaker[AsyncSession],
    asset_sm: async_sessionmaker[AsyncSession],
) -> None:
    """6 个 worker 并发抢 24 个任务：全部完成、无重复执行、无重复资产。

    每个 worker **持续排空**而不是只跑一轮：单轮只取 ``BATCH_LIMIT`` 条，
    且并发启动的多个 worker 看到的是同一个"最早的 16 条"窗口——只跑一轮
    测的是窗口大小，不是吞吐。排空循环才是 worker 的真实运行形态。
    """
    project = _new_project("load")
    route_id = new_route_id()
    tasks = [
        await seed_task(
            gen_sm,
            entity_id=f"gen_load_{uuid.uuid4().hex[:8]}",
            route_id=route_id,
            project_id=project,
            status=GenerationTaskStatus.PENDING,
        )
        for _ in range(TASK_COUNT)
    ]

    async def _worker(index: int) -> GenerationRoundStats:
        total = GenerationRoundStats()
        # 上界只是防死循环：正常情况下 24 个任务 2 轮内排空。
        for _ in range(8):
            stats = await execute_round(
                gen_sm,
                _context(project, f"load-worker-{index}"),
                DeterministicGenerationAdapter(),
                GenerationRoundStats(),
                registrar=AssetVersionRegistrar(asset_sessionmaker=asset_sm),
                lease_seconds=_LEASE_SECONDS,
            )
            total.claimed += stats.claimed
            total.reclaimed += stats.reclaimed
            total.succeeded += stats.succeeded
            total.failed += stats.failed
            total.skipped += stats.skipped
            if await _open_task_count(gen_sm, project_id=project) == 0:
                break
        return total

    started = time.perf_counter()
    rounds = await asyncio.gather(*(_worker(i) for i in range(WORKER_COUNT)))
    elapsed = time.perf_counter() - started

    succeeded = sum(stats.succeeded for stats in rounds)
    claimed = sum(stats.claimed for stats in rounds)
    # exactly-once：成功执行数恰好等于任务数——多一个就是重复执行。
    assert succeeded == TASK_COUNT, [s.__dict__ for s in rounds]
    assert claimed == TASK_COUNT

    async with gen_sm() as session:
        await apply_db_context(session, project_ids=(project,))
        repo = SqlAlchemyGenerationTaskRepository(session)
        stored = [await repo.get_by_id(task.entity_id) for task in tasks]

    assert all(task is not None for task in stored)
    assert all(
        task.status is GenerationTaskStatus.SUCCEEDED for task in stored if task
    )
    # 每个任务的终态都指向**自己的**资产版本：串号在结果引用上就暴露。
    assert {
        task.result_ref
        for task in stored
        if task is not None
    } == {derive_asset_version_id(task) for task in tasks}
    assert len({task.result_ref for task in stored if task}) == TASK_COUNT

    asset_count = await _asset_version_count(asset_sm, project_id=project)
    assert asset_count == TASK_COUNT

    throughput = TASK_COUNT / elapsed if elapsed else 0.0
    record(
        "generation_concurrent_exactly_once",
        task_count=TASK_COUNT,
        worker_count=WORKER_COUNT,
        elapsed_seconds=round(elapsed, 3),
        tasks_per_second=round(throughput, 3),
        claimed=claimed,
        succeeded=succeeded,
        skipped=sum(stats.skipped for stats in rounds),
        asset_versions=asset_count,
        invariant="succeeded == task_count && asset_versions == task_count",
    )


async def test_generation_request_accept_latency_under_concurrency(
    auth_client: AsyncClient,
    gen_sm: async_sessionmaker[AsyncSession],
    shot_sm: async_sessionmaker[AsyncSession],
) -> None:
    """20 个并发受理：全部 PENDING，延迟分布落证据（p50/p95/max）。"""
    project = _new_project("accept")
    route_id = new_route_id()
    await seed_route(gen_sm, route_id=route_id)
    spec_id, vplan_id = await seed_shot_refs(shot_sm, project_id=project)
    headers = make_test_headers(subject_id="director_load", roles=["导演"])

    async def _post(body: dict[str, Any]) -> tuple[int, float]:
        request_started = time.perf_counter()
        response = await auth_client.post(
            "/actions/generation.request", headers=headers, json=body
        )
        return response.status_code, time.perf_counter() - request_started

    # 串行基线：没有它，并发 p95 无法解释（是请求本身慢，还是排队慢）。
    baseline_project = _new_project("accept_base")
    base_spec, base_vplan = await seed_shot_refs(shot_sm, project_id=baseline_project)
    baseline_ms: list[float] = []
    for _ in range(5):
        status, latency = await _post(
            request_body(
                route_id=route_id,
                project_id=baseline_project,
                spec_ref=base_spec,
                vplan_ref=base_vplan,
            )
        )
        assert status == 200
        baseline_ms.append(latency * 1000)

    async def _accept() -> tuple[int, float]:
        body: dict[str, Any] = request_body(
            route_id=route_id, project_id=project, spec_ref=spec_id, vplan_ref=vplan_id
        )
        return await _post(body)

    started = time.perf_counter()
    results = await asyncio.gather(*(_accept() for _ in range(ACCEPT_CONCURRENCY)))
    elapsed = time.perf_counter() - started

    assert [status for status, _ in results] == [200] * ACCEPT_CONCURRENCY
    latencies_ms = [round(latency * 1000, 2) for _, latency in results]

    async with gen_sm() as session:
        await apply_db_context(session, project_ids=(project,))
        pending = await session.scalar(
            text(
                "SELECT count(*) FROM generation_execution.generation_tasks "
                "WHERE project_id = :pid AND status = 'PENDING'"
            ),
            {"pid": project},
        )
        events = await session.scalar(
            text(
                "SELECT count(*) FROM outbox.outbox_events "
                "WHERE event_type = 'GENERATION_REQUESTED' "
                "AND payload->>'project_id' = :pid"
            ),
            {"pid": project},
        )
    # 一请求一任务一事件：并发下不丢、不合并、不串项目。
    assert int(pending or 0) == ACCEPT_CONCURRENCY
    assert int(events or 0) == ACCEPT_CONCURRENCY

    record(
        "generation_request_accept_latency",
        concurrency=ACCEPT_CONCURRENCY,
        sequential_p50_ms=round(percentile(baseline_ms, 0.50), 2),
        elapsed_seconds=round(elapsed, 3),
        requests_per_second=round(
            ACCEPT_CONCURRENCY / elapsed if elapsed else 0.0, 3
        ),
        p50_ms=round(percentile(latencies_ms, 0.50), 2),
        p95_ms=round(percentile(latencies_ms, 0.95), 2),
        max_ms=max(latencies_ms),
        accepted=ACCEPT_CONCURRENCY,
        pending_tasks=int(pending or 0),
        outbox_events=int(events or 0),
    )


async def test_replayed_registration_keeps_single_asset_version(
    gen_sm: async_sessionmaker[AsyncSession],
    asset_sm: async_sessionmaker[AsyncSession],
) -> None:
    """重放登记（"登记成功、终态失败"后的重跑）不新增也不覆盖资产版本。"""
    project = _new_project("replay")
    task = await seed_task(
        gen_sm,
        entity_id=f"gen_replay_{uuid.uuid4().hex[:8]}",
        route_id=new_route_id(),
        project_id=project,
        status=GenerationTaskStatus.PENDING,
    )
    context = _context(project, "replay-worker")
    adapter = DeterministicGenerationAdapter()
    registrar = AssetVersionRegistrar(asset_sessionmaker=asset_sm)
    output = await adapter.execute(task)
    principal = context.to_principal()

    first = await registrar.register(task=task, output=output, principal=principal)
    second = await registrar.register(task=task, output=output, principal=principal)

    assert first.asset_version_id == second.asset_version_id == derive_asset_version_id(
        task
    )
    assert await _asset_version_count(asset_sm, project_id=project) == 1

    record(
        "generation_registration_replay_idempotent",
        replays=2,
        asset_versions=1,
        asset_version_id=first.asset_version_id,
    )
