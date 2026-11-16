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
from packages.shared.db_roles import DatabaseDomain
from packages.shared.outbox_writer import record_event
from packages.shared.rls import set_tenant_context
from workers.relay.consumer import OutboxEvent, OutboxRelay
from workers.relay.errors import DeliveryDeferredError

TENANT = uuid.uuid4()


def _project_sm() -> async_sessionmaker[AsyncSession]:
    """项目规划域会话工厂（业务侧追加事件）。"""
    return app.state.pools.sessionmaker(DatabaseDomain.PROJECT_PLANNING)


def _relay_sm() -> async_sessionmaker[AsyncSession]:
    """投递域会话工厂（relay 领取 / 回写状态）。"""
    return app.state.pools.sessionmaker(DatabaseDomain.OUTBOX_RELAY)


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
    project_sm = _project_sm()
    relay_sm = _relay_sm()
    event_id = await _append_event(project_sm)
    relay = OutboxRelay(relay_sm, {"SCRIPT_LOCK_APPROVED": _failing_handler})

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
    project_sm = _project_sm()
    relay_sm = _relay_sm()
    event_id = await _append_event(project_sm)
    relay = OutboxRelay(
        relay_sm, {"SCRIPT_LOCK_APPROVED": _failing_handler}, max_attempts=1
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


async def test_dead_letter_not_counted_in_backlog(admin_engine: AsyncEngine) -> None:
    """事件进入死信后，list_active_tenants 的 pending_count 归零、dead_count 为 1。

    复核 R2：死信不得计入 backlog——否则 OUTBOX_RELAY_BACKLOG 报警永远消不掉，
    租户也永远留在扫描列表里每轮白扫。证明枚举函数的口径与 _CLAIM 领取口径一致。
    """
    project_sm = _project_sm()
    relay_sm = _relay_sm()
    await _append_event(project_sm)
    relay = OutboxRelay(
        relay_sm, {"SCRIPT_LOCK_APPROVED": _failing_handler}, max_attempts=1
    )
    await relay.drain_tenant(TENANT)

    async with admin_engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT pending_count, dead_count "
                    "FROM outbox.list_active_tenants() WHERE tenant_id = :t"
                ),
                {"t": TENANT},
            )
        ).one()
    assert int(row[0]) == 0, "死信不得计入 pending_count（backlog）"
    assert int(row[1]) == 1, "死信必须单独计入 dead_count"


async def _deferred_handler(event: OutboxEvent) -> None:
    """模拟端点熔断 OPEN：明确知道本次没有外发。"""
    raise DeliveryDeferredError("webhook 端点熔断开启，快速失败（未外发）")


async def test_deferred_delivery_does_not_consume_attempts(
    admin_engine: AsyncEngine,
) -> None:
    """「没外发」不得被记成一次失败尝试（审查 N2）。

    熔断 OPEN 期间被拦下的事件从未真正投递；若照常 attempts+1，几轮之后就会
    撞到 max_attempts 变成死信——一批从未尝试过的事件被"失败"掉，直接违背
    ADR-R13「事件不丢弃」。这里用 max_attempts=1 把口径钉死：只要被计一次，
    第一轮就会死信。
    """
    project_sm = _project_sm()
    relay_sm = _relay_sm()
    first = await _append_event(project_sm)
    second = await _append_event(project_sm)
    relay = OutboxRelay(
        relay_sm, {"SCRIPT_LOCK_APPROVED": _deferred_handler}, max_attempts=1
    )

    for _ in range(3):
        stats = await relay.drain_tenant(TENANT)
        # 端点级拦截对整批同样成立：本轮只记一次推迟就停止，不再逐条撞闸门。
        assert stats.deferred == 1
        assert stats.failed == 0
        assert stats.dead_lettered == 0

    for event_id in (first, second):
        attempts, _next_at, dead_at, published = await _state(admin_engine, event_id)
        assert attempts == 0, "未外发的轮次不得递增 attempts"
        assert dead_at is None, "未外发的事件不得推进死信"
        assert published is None, "未外发的事件不得 ack"
