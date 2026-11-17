"""生成任务租约与崩溃恢复集成测试（真实 PostgreSQL，域角色 + RLS）。

要证明的四件事：
1. 领取把租约落到**数据库行**（不是内存标记）——进程崩溃后它仍然在；
2. 租约过期后另一个 worker 能接管并完成任务，且接管被计量（reclaimed）；
3. 租约有效期内不会被抢（否则"崩溃恢复"会退化成"重复执行"）；
4. 栅栏双向成立：被接管的旧持有者，无论拿旧快照还是重读，都写不进终态。

租约过期的模拟方式刻意用 SQL 把 ``lease_expires_at`` 推到过去，而不是
sleep：测试要验证的是"过期之后谁能接管"，不是"时钟走得准不准"。该列在
守卫可变白名单内，这是运行态元数据的正常写入路径。
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.errors.error_codes import DomainError
from services.asset_service.repository.sqlalchemy_asset_repo import (
    SqlAlchemyAssetRepository,
)
from services.generation_execution.domain.enums import GenerationTaskStatus
from services.generation_execution.domain.generation_task import GenerationTask
from services.generation_execution.repository.generation_task_sqlalchemy_repo import (
    SqlAlchemyGenerationTaskRepository,
)
from services.generation_execution.service.generation_service import (
    GenerationTaskLifecycle,
)
from tests.conftest import TEST_TENANT_ID, apply_db_context
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

pytestmark = pytest.mark.asyncio

_LEASE_SECONDS = 60.0


def _new_project() -> str:
    """每测试唯一项目：worker 轮询按项目范围（RLS）过滤，避免残留任务干扰。"""
    return f"proj_lease_{uuid.uuid4().hex[:8]}"


async def _seed_pending(
    gen_sm: async_sessionmaker[AsyncSession], *, project_id: str
) -> GenerationTask:
    task = GenerationTask(
        entity_id=f"gen_lease_{uuid.uuid4().hex[:10]}",
        project_id=project_id,
        shot_id="SHOT_001",
        route_id=f"route_{uuid.uuid4().hex[:6]}",
        shot_spec_ref="spec_x",
        visual_plan_ref="vplan_x",
        constraint_snapshot_ref="NOT_APPLICABLE",
        budget_reservation_ref="budget_res_x",
        quota_reservation_ref="quota_res_x",
        dependency_refs=[],
        attempt=1,
        created_by="director_001",
        tenant_id=TEST_TENANT_ID,
    )
    async with gen_sm() as session:
        await apply_db_context(session, project_ids=(project_id,))
        await SqlAlchemyGenerationTaskRepository(session).add(task)
        await session.commit()
    return task


async def _claim(
    gen_sm: async_sessionmaker[AsyncSession],
    *,
    project_id: str,
    task_id: str,
    worker_id: str,
    lease_seconds: float = _LEASE_SECONDS,
) -> GenerationTask:
    async with gen_sm() as session:
        await apply_db_context(session, project_ids=(project_id,))
        repo = SqlAlchemyGenerationTaskRepository(session)
        task = await repo.get_by_id(task_id)
        assert task is not None
        await GenerationTaskLifecycle(repo).claim(
            task,
            actor="svc_generation_executor",
            lease_owner=worker_id,
            lease_seconds=lease_seconds,
        )
        await session.commit()
        return task


async def _expire_lease(
    gen_sm: async_sessionmaker[AsyncSession], *, project_id: str, task_id: str
) -> None:
    """把租约推到过去（模拟持有者崩溃、不再续租）。"""
    async with gen_sm() as session:
        await apply_db_context(session, project_ids=(project_id,))
        await session.execute(
            text(
                "UPDATE generation_execution.generation_tasks "
                "SET lease_expires_at = now() - interval '1 second' "
                "WHERE entity_id = :tid"
            ),
            {"tid": task_id},
        )
        await session.commit()


async def _reload(
    gen_sm: async_sessionmaker[AsyncSession], *, project_id: str, task_id: str
) -> GenerationTask:
    async with gen_sm() as session:
        await apply_db_context(session, project_ids=(project_id,))
        task = await SqlAlchemyGenerationTaskRepository(session).get_by_id(task_id)
    assert task is not None
    return task


def _context(project_id: str, worker_id: str) -> GenerationExecutorContext:
    return GenerationExecutorContext(
        tenant_id=TEST_TENANT_ID,
        organization_id=TEST_TENANT_ID,
        project_ids=frozenset({project_id}),
        worker_id=worker_id,
    )


async def test_claim_persists_lease_on_row(
    gen_sm: async_sessionmaker[AsyncSession],
) -> None:
    """领取后租约在数据库行上：进程崩溃，租约仍然可被他人观察到。"""
    project = _new_project()
    seeded = await _seed_pending(gen_sm, project_id=project)
    await _claim(gen_sm, project_id=project, task_id=seeded.entity_id, worker_id="host-a:1")

    stored = await _reload(gen_sm, project_id=project, task_id=seeded.entity_id)
    assert stored.status is GenerationTaskStatus.RUNNING
    assert stored.lease_owner == "host-a:1"
    assert stored.lease_expires_at is not None
    assert stored.started_at is not None


async def test_expired_lease_reclaimed_and_task_completed(
    gen_sm: async_sessionmaker[AsyncSession],
    asset_sm: async_sessionmaker[AsyncSession],
) -> None:
    """崩溃恢复：租约过期 → 另一 worker 接管 → 任务完成、产出登记。"""
    project = _new_project()
    seeded = await _seed_pending(gen_sm, project_id=project)
    await _claim(
        gen_sm, project_id=project, task_id=seeded.entity_id, worker_id="host-crashed:9"
    )
    await _expire_lease(gen_sm, project_id=project, task_id=seeded.entity_id)

    stats = await execute_round(
        gen_sm,
        _context(project, "host-b:2"),
        DeterministicGenerationAdapter(),
        GenerationRoundStats(),
        registrar=AssetVersionRegistrar(asset_sessionmaker=asset_sm),
        lease_seconds=_LEASE_SECONDS,
    )
    assert (stats.reclaimed, stats.claimed, stats.succeeded) == (1, 0, 1)

    stored = await _reload(gen_sm, project_id=project, task_id=seeded.entity_id)
    assert stored.status is GenerationTaskStatus.SUCCEEDED
    # 归属已转移：新的持有者完成终态。
    assert stored.lease_owner == "host-b:2"
    assert stored.result_ref == derive_asset_version_id(seeded)
    assert stored.completed_at is not None

    async with asset_sm() as session:
        await apply_db_context(session, project_ids=(project,))
        assert (
            await SqlAlchemyAssetRepository(session).get_by_id(
                derive_asset_version_id(seeded)
            )
            is not None
        )


async def test_valid_lease_is_not_stolen(
    gen_sm: async_sessionmaker[AsyncSession],
    asset_sm: async_sessionmaker[AsyncSession],
) -> None:
    """租约未到期：另一个 worker 不能接管（否则就是并发重复执行）。"""
    project = _new_project()
    seeded = await _seed_pending(gen_sm, project_id=project)
    await _claim(gen_sm, project_id=project, task_id=seeded.entity_id, worker_id="host-a:1")

    stats = await execute_round(
        gen_sm,
        _context(project, "host-b:2"),
        DeterministicGenerationAdapter(),
        GenerationRoundStats(),
        registrar=AssetVersionRegistrar(asset_sessionmaker=asset_sm),
        lease_seconds=_LEASE_SECONDS,
    )
    assert (stats.claimed, stats.reclaimed, stats.succeeded) == (0, 0, 0)

    stored = await _reload(gen_sm, project_id=project, task_id=seeded.entity_id)
    assert stored.status is GenerationTaskStatus.RUNNING
    assert stored.lease_owner == "host-a:1"


async def test_zombie_writer_fenced_after_takeover(
    gen_sm: async_sessionmaker[AsyncSession],
) -> None:
    """被接管的旧持有者写不进终态：旧快照被版本锁挡，重读被租约持有者挡。"""
    project = _new_project()
    seeded = await _seed_pending(gen_sm, project_id=project)
    zombie_snapshot = await _claim(
        gen_sm, project_id=project, task_id=seeded.entity_id, worker_id="host-a:1"
    )
    await _expire_lease(gen_sm, project_id=project, task_id=seeded.entity_id)

    # 另一 worker 接管（只写数据库行，不改写 zombie 的快照）。
    async with gen_sm() as session:
        await apply_db_context(session, project_ids=(project,))
        repo = SqlAlchemyGenerationTaskRepository(session)
        fresh = await repo.get_by_id(seeded.entity_id)
        assert fresh is not None
        await GenerationTaskLifecycle(repo).take_over(
            fresh,
            actor="svc_generation_executor",
            lease_owner="host-b:2",
            lease_seconds=_LEASE_SECONDS,
        )
        await session.commit()

    # 1) 旧快照 + 旧版本：乐观锁拒绝。
    with pytest.raises(DomainError) as stale_exc:
        async with gen_sm() as session:
            await apply_db_context(session, project_ids=(project,))
            await GenerationTaskLifecycle(
                SqlAlchemyGenerationTaskRepository(session)
            ).succeed(
                zombie_snapshot,
                result_ref="av_stale",
                actor="svc_generation_executor",
                lease_owner="host-a:1",
            )
    assert stale_exc.value.error_code.code == "CONCURRENT_MODIFICATION"

    # 2) 重读后再写：租约持有者不是自己，栅栏拒绝（执行环的真实路径）。
    fresh_snapshot = await _reload(gen_sm, project_id=project, task_id=seeded.entity_id)
    with pytest.raises(DomainError) as holder_exc:
        async with gen_sm() as session:
            await apply_db_context(session, project_ids=(project,))
            await GenerationTaskLifecycle(
                SqlAlchemyGenerationTaskRepository(session)
            ).succeed(
                fresh_snapshot,
                result_ref="av_stale",
                actor="svc_generation_executor",
                lease_owner="host-a:1",
            )
    assert holder_exc.value.error_code.code == "CONCURRENT_MODIFICATION"
    assert "栅栏" in str(holder_exc.value)

    stored = await _reload(gen_sm, project_id=project, task_id=seeded.entity_id)
    assert stored.status is GenerationTaskStatus.RUNNING
    assert stored.result_ref is None
    assert stored.lease_owner == "host-b:2"


async def test_renew_extends_only_for_holder(
    gen_sm: async_sessionmaker[AsyncSession],
) -> None:
    """续租：持有者可延长；换人续租被拒（避免"帮别人续命"）。"""
    project = _new_project()
    seeded = await _seed_pending(gen_sm, project_id=project)
    await _claim(gen_sm, project_id=project, task_id=seeded.entity_id, worker_id="host-a:1")
    before = await _reload(gen_sm, project_id=project, task_id=seeded.entity_id)

    async with gen_sm() as session:
        await apply_db_context(session, project_ids=(project,))
        repo = SqlAlchemyGenerationTaskRepository(session)
        task = await repo.get_by_id(seeded.entity_id)
        assert task is not None
        await GenerationTaskLifecycle(repo).renew(
            task, lease_owner="host-a:1", lease_seconds=600
        )
        await session.commit()

    after = await _reload(gen_sm, project_id=project, task_id=seeded.entity_id)
    assert after.lease_expires_at is not None
    assert before.lease_expires_at is not None
    assert after.lease_expires_at > before.lease_expires_at

    with pytest.raises(DomainError) as exc_info:
        async with gen_sm() as session:
            await apply_db_context(session, project_ids=(project,))
            repo = SqlAlchemyGenerationTaskRepository(session)
            task = await repo.get_by_id(seeded.entity_id)
            assert task is not None
            await GenerationTaskLifecycle(repo).renew(
                task, lease_owner="host-b:2", lease_seconds=600
            )
    assert exc_info.value.error_code.code == "CONCURRENT_MODIFICATION"
