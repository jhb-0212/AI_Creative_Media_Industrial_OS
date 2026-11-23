"""R42 T0→T3 端到端验收（真实 PostgreSQL）。

这里证明的是「四段各自提交、跨域只靠事件」的整体行为，而不是服务方法能被调用：
T0 意图先落库、T1 确认后任务进入等待态并发出登记请求、T2 用资产域角色建资产与
不可变回执、T3 再用生成域角色按回执写终态。重复投递不得产生第二个资产或第二条
终态事件。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from packages.shared.database import (
    build_migration_url,
    create_asset_service_engine,
    create_production_planning_engine,
    create_sessionmaker,
)
from packages.shared.rls import set_tenant_context
from packages.shared.storage_locator import build_locator
from services.production_planning.repository.sqlalchemy_repos import (
    SqlAlchemyGenerationTaskRepository,
)
from services.production_planning.service.generation_service import GenerationService
from services.project_planning.repository.sqlalchemy_outbox_writer import (
    SqlAlchemyOutboxWriter,
)
from workers.relay.asset_registration import build_asset_registration_handlers
from workers.relay.consumer import OutboxEvent

_TENANT = UUID("00000000-0000-0000-0000-000000000041")
_DIGEST = "e" * 64
_LOCATOR = build_locator(_DIGEST, "png")


@pytest.fixture(scope="module")
async def admin_engine() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(build_migration_url(), pool_pre_ping=True)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture(scope="module")
async def prod_sm() -> AsyncIterator[object]:
    engine = create_production_planning_engine()
    try:
        yield create_sessionmaker(engine)
    finally:
        await engine.dispose()


@pytest.fixture(scope="module")
async def asset_sm() -> AsyncIterator[object]:
    engine = create_asset_service_engine()
    try:
        yield create_sessionmaker(engine)
    finally:
        await engine.dispose()


async def _seed(admin_engine: AsyncEngine) -> dict[str, object]:
    run = uuid4()
    project_id = f"saga-proj-{run}"
    task_id = f"saga-task-{run}"
    task_entity = f"saga-entity-{run}"
    now = datetime.now(UTC)
    async with admin_engine.begin() as conn:
        await conn.execute(
            sa.text(
                "INSERT INTO project_planning.projects "
                "(entity_id, entity_type, schema_version, entity_version, status, "
                " owner_module, tenant_id, created_at, created_by, updated_at, "
                " project_name, project_tier, organization_id) "
                "VALUES (:pid, 'Project', 205, 1, 'DRAFT', 'project_planning', "
                " :tid, :now, 'e2e-seeder', :now, 'Saga 端到端', 'B', NULL)"
            ),
            {"pid": project_id, "tid": _TENANT, "now": now},
        )
        await conn.execute(
            sa.text(
                "INSERT INTO production_planning.generation_tasks "
                "(entity_id, entity_type, schema_version, entity_version, status, "
                " owner_module, tenant_id, created_at, created_by, updated_at, "
                " generation_task_id, project_id, shot_id, shot_revision_id, "
                " production_route_ref, model_adapter_ref, constraint_snapshot_id, "
                " budget_ref, prompt_ref, lease_owner, lease_expires_at, attempt_count, "
                " asset_version_id, quality_profile_version) "
                "VALUES (:eid, 'GenerationTask', 205, 1, 'RUNNING', "
                " 'production_planning', :tid, :now, 'e2e-seeder', :now, "
                " :task_id, :pid, 'shot-1', 'rev-1', 'route-1', 'adapter-1', 'snap-1', "
                " 'budget-1', 'inline:lighthouse', 'worker-e2e', :expiry, 1, '', 'qp-e2e')"
            ),
            {
                "eid": task_entity,
                "tid": _TENANT,
                "now": now,
                "task_id": task_id,
                "pid": project_id,
                "expiry": now.replace(year=now.year + 1),
            },
        )
    return {
        "run": run,
        "project_id": project_id,
        "task_id": task_id,
        "task_entity": task_entity,
    }


@pytest.fixture(scope="module")
async def seed(admin_engine: AsyncEngine) -> AsyncIterator[dict[str, object]]:
    """失败路径也必须复位等待态：CI 在 pytest 之后还要 downgrade -1。"""
    data = await _seed(admin_engine)
    try:
        yield data
    finally:
        async with admin_engine.begin() as conn:
            await conn.execute(
                sa.text(
                    "UPDATE production_planning.generation_tasks "
                    "SET status = 'SUCCEEDED' "
                    "WHERE generation_task_id = :task "
                    "AND status = 'AWAITING_ASSET_REGISTRATION'"
                ),
                {"task": data["task_id"]},
            )


async def _read_outbox_event(
    admin_engine: AsyncEngine, event_type: str, task_id: str
) -> OutboxEvent:
    async with admin_engine.connect() as conn:
        row = (
            await conn.execute(
                sa.text(
                    "SELECT event_id, aggregate_type, aggregate_id, tenant_id, "
                    "event_type, payload, occurred_at FROM outbox.outbox_events "
                    "WHERE event_type = :etype AND tenant_id = :tid "
                    "AND payload->>'generation_task_id' = :task "
                    "ORDER BY occurred_at DESC LIMIT 1"
                ),
                {"etype": event_type, "tid": _TENANT, "task": task_id},
            )
        ).one()
    return OutboxEvent(
        event_id=row.event_id,
        aggregate_type=row.aggregate_type,
        aggregate_id=row.aggregate_id,
        tenant_id=row.tenant_id,
        event_type=row.event_type,
        payload=dict(row.payload),
        occurred_at=row.occurred_at,
    )


async def _count(admin_engine: AsyncEngine, sql: str, params: dict[str, object]) -> int:
    async with admin_engine.connect() as conn:
        return int(await conn.scalar(sa.text(sql), params) or 0)


async def test_full_saga_commits_and_replays_idempotently(
    admin_engine: AsyncEngine, prod_sm: object, asset_sm: object, seed: dict[str, object]
) -> None:
    task_id = str(seed["task_id"])
    task_entity = str(seed["task_entity"])
    attempt_id = f"attempt-{seed['run']}"

    # T0 + T1：两次独立提交，中间没有跨越对象存储 I/O 的事务。
    async with prod_sm() as session:  # type: ignore[operator]
        await set_tenant_context(session, _TENANT)
        service = GenerationService(
            SqlAlchemyGenerationTaskRepository(session), SqlAlchemyOutboxWriter(session)
        )
        await service.prepare_artifact(
            task_id=task_entity,
            worker_id="worker-e2e",
            locator=_LOCATOR,
            sha256=_DIGEST,
            attempt_id=attempt_id,
        )
        await session.commit()
    assert await _count(
        admin_engine,
        "SELECT count(*) FROM production_planning.artifact_manifests "
        "WHERE status='PREPARED' AND generation_task_id=:task",
        {"task": task_id},
    ) == 1

    async with prod_sm() as session:  # type: ignore[operator]
        await set_tenant_context(session, _TENANT)
        service = GenerationService(
            SqlAlchemyGenerationTaskRepository(session), SqlAlchemyOutboxWriter(session)
        )
        await service.confirm_artifact(
            task_id=task_entity,
            worker_id="worker-e2e",
            attempt_id=attempt_id,
            locator=_LOCATOR,
            sha256=_DIGEST,
            media_type="image/png",
        )
        await session.commit()

    assert await _count(
        admin_engine,
        "SELECT count(*) FROM production_planning.generation_tasks "
        "WHERE entity_id=:eid AND status='AWAITING_ASSET_REGISTRATION'",
        {"eid": task_entity},
    ) == 1
    requested = await _read_outbox_event(
        admin_engine, "ASSET_REGISTRATION_REQUESTED", task_id
    )
    assert requested.payload["sha256"] == _DIGEST

    handlers = build_asset_registration_handlers(
        production_planning_sessionmaker=prod_sm, asset_sessionmaker=asset_sm
    )
    # T2：重复投递只建一个资产与一条回执。
    await handlers["ASSET_REGISTRATION_REQUESTED"](requested)
    await handlers["ASSET_REGISTRATION_REQUESTED"](requested)
    assert await _count(
        admin_engine,
        "SELECT count(*) FROM asset_service.asset_registration_receipts "
        "WHERE generation_task_id=:task",
        {"task": task_id},
    ) == 1
    assert await _count(
        admin_engine,
        "SELECT count(*) FROM asset_service.asset_versions av "
        "JOIN asset_service.asset_registration_receipts r "
        "ON av.entity_id = r.asset_version_id "
        "WHERE r.generation_task_id = :task AND av.sha256 = :digest",
        {"task": task_id, "digest": _DIGEST},
    ) == 1

    # T3：终态的资产 ID 必须来自回执，重复消费不重复发成功事件。
    completed = await _read_outbox_event(
        admin_engine, "ASSET_REGISTRATION_COMPLETED", task_id
    )
    await handlers["ASSET_REGISTRATION_COMPLETED"](completed)
    await handlers["ASSET_REGISTRATION_COMPLETED"](completed)
    assert await _count(
        admin_engine,
        "SELECT count(*) FROM production_planning.generation_tasks "
        "WHERE entity_id=:eid AND status='SUCCEEDED' AND asset_version_id<>''",
        {"eid": task_entity},
    ) == 1
    assert await _count(
        admin_engine,
        "SELECT count(*) FROM outbox.outbox_events WHERE event_type='GENERATION_SUCCEEDED' "
        "AND payload->>'generation_task_id' = :task",
        {"task": task_id},
    ) == 1
