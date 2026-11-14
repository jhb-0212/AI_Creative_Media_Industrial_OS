"""Outbox relay 重试与死信行为（审计 P2-2）。

要证明的三件事：
1. handler 抛异常后事件不 ack，attempts 累加，next_attempt_at 后退；
2. 退避期内不会被再次领取（毒事件不会每轮都被立刻重试）；
3. 达到 max_attempts 进入死信，此后不再被领取，但数据保留可查。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from apps.api.main import app
from packages.shared.database import build_migration_url
from packages.shared.outbox_writer import record_event
from packages.shared.rls import set_tenant_context
from workers.relay.consumer import OutboxEvent, OutboxRelay

TENANT = uuid.uuid4()


@pytest.fixture(scope="module")
async def admin_engine() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(build_migration_url())
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture(autouse=True)
async def clean_events(admin_engine: AsyncEngine) -> AsyncIterator[None]:
    yield
    async with admin_engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM outbox.outbox_events WHERE tenant_id = :t"),
            {"t": TENANT},
        )


async def _append_event(sm: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    async with sm() as session:
        await set_tenant_context(session, TENANT)
        event_id = record_event(
            session,
            aggregate_type="ScriptRevision",
            aggregate_id="agg-retry",
            tenant_id=TENANT,
            event_type="SCRIPT_LOCK_APPROVED",
            payload={"probe": "retry"},
        )
        await session.commit()
    return event_id


async def _state(
    admin_engine: AsyncEngine, event_id: uuid.UUID
) -> tuple[int, object, object, object]:
    async with admin_engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT attempts, next_attempt_at, dead_lettered_at, published_at "
                    "FROM outbox.outbox_events WHERE event_id = :e"
                ),
                {"e": event_id},
            )
        ).one()
    return int(row[0]), row[1], row[2], row[3]


async def _failing_handler(event: OutboxEvent) -> None:
    raise RuntimeError("poison event")


async def test_failure_backs_off_and_is_not_reclaimed(
    admin_engine: AsyncEngine,
) -> None:
    sm = app.state.sessionmaker
    event_id = await _append_event(sm)
    relay = OutboxRelay(sm, {"SCRIPT_LOCK_APPROVED": _failing_handler})

    stats = await relay.drain_tenant(TENANT)
    assert stats.failed == 1
    assert stats.dead_lettered == 0

    attempts, next_at, dead_at, published = await _state(admin_engine, event_id)
    assert attempts == 1
    assert published is None, "失败事件不得 ack"
    assert dead_at is None
    # 退避已生效：紧接着再跑一轮，事件还没到 next_attempt_at，不应被领取。
    assert next_at is not None

    second = await relay.drain_tenant(TENANT)
    assert second.failed == 0
    assert second.delivered == 0


async def test_max_attempts_marks_dead_letter(admin_engine: AsyncEngine) -> None:
    sm = app.state.sessionmaker
    event_id = await _append_event(sm)
    relay = OutboxRelay(
        sm, {"SCRIPT_LOCK_APPROVED": _failing_handler}, max_attempts=1
    )

    stats = await relay.drain_tenant(TENANT)
    assert stats.failed == 1
    assert stats.dead_lettered == 1

    attempts, _next_at, dead_at, published = await _state(admin_engine, event_id)
    assert attempts == 1
    assert dead_at is not None, "达到 max_attempts 必须进入死信"
    assert published is None

    # 死信不再被领取：再跑一轮既不投递也不失败。
    again = await relay.drain_tenant(TENANT)
    assert again.failed == 0
    assert again.delivered == 0

    # 死信保留可查（不删除：投递放弃本身是可审计事实）。
    async with admin_engine.connect() as conn:
        count = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM outbox.outbox_events "
                    "WHERE event_id = :e AND dead_lettered_at IS NOT NULL"
                ),
                {"e": event_id},
            )
        ).scalar_one()
    assert count == 1
