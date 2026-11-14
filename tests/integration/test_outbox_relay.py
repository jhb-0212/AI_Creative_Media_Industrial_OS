"""Outbox relay 多租户投递测试（R06 方案 B 的核心不变量）。

要证明的三件事（缺一条方案 B 就不成立）：
1. relay 用 app_role，单租户上下文内**只能**读到本租户事件——越界的不是
   「代码自觉」，而是 RLS 拒绝；
2. 没有处理器的事件保持未投递（不 ack），积压不消失；
3. 处理器失败的事件同样保持未投递，且不影响同批/其他租户。

清理用超级用户连接：`outbox.outbox_events` 对 app_role 已撤销 DELETE
（append-only，迁移 d4e5f6a7b8c9）。测试清理本身就是该不变量的一个证明。
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

TENANT_A = uuid.uuid4()
TENANT_B = uuid.uuid4()


@pytest.fixture(scope="module")
async def admin_engine() -> AsyncIterator[AsyncEngine]:
    """迁移角色连接（仅用于清理；生产代码不接触）。"""
    engine = create_async_engine(build_migration_url())
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture(autouse=True)
async def clean_events(admin_engine: AsyncEngine) -> AsyncIterator[None]:
    """每个用例前后清空本用例租户的事件（app_role 无 DELETE，故用迁移角色）。"""
    yield
    async with admin_engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM outbox.outbox_events WHERE tenant_id IN (:a, :b)"),
            {"a": TENANT_A, "b": TENANT_B},
        )


async def _append_event(
    sm: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    event_type: str,
    aggregate_id: str,
) -> uuid.UUID:
    """在租户上下文内追加一条事件（走生产写入路径 record_event + UoW 提交）。"""
    async with sm() as session:
        await set_tenant_context(session, tenant_id)
        event_id = record_event(
            session,
            aggregate_type="ScriptRevision",
            aggregate_id=aggregate_id,
            tenant_id=tenant_id,
            event_type=event_type,
            payload={"to_status": "LOCKED"},
        )
        await session.commit()
    return event_id


async def _published_at(
    sm: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, event_id: uuid.UUID
) -> object:
    """在指定租户上下文内读事件的投递时间（返回 None 表示未投递）。"""
    async with sm() as session:
        await set_tenant_context(session, tenant_id)
        return await session.scalar(
            text("SELECT published_at FROM outbox.outbox_events WHERE event_id = :e"),
            {"e": event_id},
        )


async def test_drain_only_reads_own_tenant() -> None:
    """租户 A 的事件被投递；租户 B 的事件不受影响（RLS 挡住 A 的读取）。"""
    sm = app.state.sessionmaker
    event_a = await _append_event(
        sm, tenant_id=TENANT_A, event_type="SCRIPT_LOCK_APPROVED", aggregate_id="agg-a"
    )
    event_b = await _append_event(
        sm, tenant_id=TENANT_B, event_type="SCRIPT_LOCK_APPROVED", aggregate_id="agg-b"
    )

    seen: list[OutboxEvent] = []

    async def handler(event: OutboxEvent) -> None:
        seen.append(event)

    relay = OutboxRelay(sm, {"SCRIPT_LOCK_APPROVED": handler})
    stats = await relay.drain_tenant(TENANT_A)

    assert stats.delivered == 1
    # 只看到 A 的事件：如果 RLS 失效，这里会同时收到 A 与 B。
    assert [e.tenant_id for e in seen] == [TENANT_A]
    assert seen[0].event_id == event_a

    assert await _published_at(sm, TENANT_A, event_a) is not None
    assert await _published_at(sm, TENANT_B, event_b) is None


async def test_unrouted_event_stays_unpublished() -> None:
    """没有处理器的事件不 ack：积压必须留痕，不能谎报已投递。"""
    sm = app.state.sessionmaker
    event_id = await _append_event(
        sm, tenant_id=TENANT_A, event_type="SHOT_GENERATED", aggregate_id="agg-x"
    )

    relay = OutboxRelay(sm, {"SCRIPT_LOCK_APPROVED": lambda e: None})
    stats = await relay.drain_tenant(TENANT_A)

    assert stats.unrouted == 1
    assert stats.delivered == 0
    assert await _published_at(sm, TENANT_A, event_id) is None


async def test_handler_failure_keeps_event_unpublished() -> None:
    """处理器抛异常 → 不 ack，事件留待下一轮重试（至少一次语义的前提）。"""
    sm = app.state.sessionmaker
    event_id = await _append_event(
        sm, tenant_id=TENANT_A, event_type="SCRIPT_LOCK_APPROVED", aggregate_id="agg-f"
    )

    async def broken_handler(event: OutboxEvent) -> None:
        raise RuntimeError("下游不可用")

    relay = OutboxRelay(sm, {"SCRIPT_LOCK_APPROVED": broken_handler})
    stats = await relay.drain_tenant(TENANT_A)

    assert stats.failed == 1
    assert stats.delivered == 0
    assert await _published_at(sm, TENANT_A, event_id) is None


async def test_default_handler_acks_when_downstream_succeeds() -> None:
    """兜底处理器成功 → 事件被 ack（这是「配置了下游」与「没有下游」的区别）。"""
    sm = app.state.sessionmaker
    event_id = await _append_event(
        sm, tenant_id=TENANT_A, event_type="SCRIPT_LOCK_APPROVED", aggregate_id="agg-d"
    )
    seen: list[OutboxEvent] = []

    async def default_handler(event: OutboxEvent) -> None:
        seen.append(event)

    # 专用处理器表为空 → 走兜底处理器（模拟统一 webhook 通道）。
    relay = OutboxRelay(sm, {}, default_handler=default_handler)
    stats = await relay.drain_tenant(TENANT_A)

    assert stats.delivered == 1
    assert [e.event_id for e in seen] == [event_id]
    assert await _published_at(sm, TENANT_A, event_id) is not None


async def test_tenant_enumeration_exposes_only_backlog_metadata() -> None:
    """枚举函数只返回「有积压的租户」与元数据，绝不返回事件负载。"""
    sm = app.state.sessionmaker
    await _append_event(
        sm, tenant_id=TENANT_B, event_type="SCRIPT_LOCK_APPROVED", aggregate_id="agg-e"
    )
    empty_tenant = uuid.uuid4()

    async with sm() as session:
        columns = (
            await session.execute(text("SELECT * FROM outbox.list_active_tenants()"))
        ).keys()
        rows = (
            await session.execute(text("SELECT * FROM outbox.list_active_tenants()"))
        ).mappings().all()

    assert set(columns) == {"tenant_id", "pending_count", "oldest_occurred_at"}
    tenant_ids = {uuid.UUID(str(row["tenant_id"])) for row in rows}
    assert TENANT_B in tenant_ids
    # 没有任何事件的租户不应出现在 relay 的扫描列表里。
    assert empty_tenant not in tenant_ids
