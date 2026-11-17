"""Outbox 崩溃重投证据（至少一次语义的**崩溃点**，ADR-R13 / R06）。

outbox 承诺"至少一次"，而这句话真正的含义只在一个很窄的窗口里体现：
处理器已经执行完、ack 还没提交，进程死了。此时：
- 事件不能丢（丢了就是审计事实永久缺失）；
- 事件**会**重复投递（这是语义，不是缺陷）——消费方必须按 event_id 幂等。

已有测试覆盖"处理器抛异常 → 不 ack → 下轮重试"，那是失败路径。本用例
刻意模拟**成功之后崩溃**：handler 正常返回，提交阶段炸掉。这是最容易
被"处理器成功即认为已投递"的实现悄悄破坏的点。
"""

from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.shared.outbox_writer import record_event
from packages.shared.rls import set_tenant_context
from tests.resilience.recording import record
from workers.relay.consumer import OutboxEvent, OutboxRelay

pytestmark = [pytest.mark.asyncio, pytest.mark.resilience]

_EVENT_TYPE = "GENERATION_REQUESTED"


def _new_tenant() -> uuid.UUID:
    """每用例唯一租户：outbox 为 append-only，用固定租户会与历史事件互相干扰。"""
    return uuid.uuid4()


async def _append_event(
    sm: async_sessionmaker[AsyncSession], *, tenant_id: uuid.UUID
) -> uuid.UUID:
    async with sm() as session:
        await set_tenant_context(session, tenant_id)
        event_id = record_event(
            session,
            aggregate_type="GenerationTask",
            aggregate_id=f"gen_{uuid.uuid4().hex[:8]}",
            tenant_id=tenant_id,
            event_type=_EVENT_TYPE,
            payload={"status": "PENDING"},
        )
        await session.commit()
    return event_id


async def _published_at(
    sm: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, event_id: uuid.UUID
) -> object:
    async with sm() as session:
        await set_tenant_context(session, tenant_id)
        return await session.scalar(
            text("SELECT published_at FROM outbox.outbox_events WHERE event_id = :e"),
            {"e": event_id},
        )


async def test_crash_after_handler_before_ack_redelivers_without_loss(
    project_sm: async_sessionmaker[AsyncSession],
    relay_sm: async_sessionmaker[AsyncSession],
) -> None:
    """处理器成功、ack 未提交时崩溃：事件不丢，下一轮重投（消费方需幂等）。"""
    tenant = _new_tenant()
    event_id = await _append_event(project_sm, tenant_id=tenant)

    delivered: list[uuid.UUID] = []

    async def handler(event: OutboxEvent) -> None:
        delivered.append(event.event_id)

    relay = OutboxRelay(relay_sm, {_EVENT_TYPE: handler})

    # 崩溃点：提交阶段抛错（等价于进程在 ack 落库前消失）。
    with patch.object(
        AsyncSession, "commit", side_effect=RuntimeError("ack 提交前进程崩溃")
    ), pytest.raises(RuntimeError):
        await relay.drain_tenant(tenant)

    # 处理器确实执行过（副作用已发生），但事件仍未投递——这正是"重复"的来源。
    assert delivered == [event_id]
    assert await _published_at(relay_sm, tenant, event_id) is None

    # 恢复后重投：同一事件第二次到达；ack 落库。
    stats = await relay.drain_tenant(tenant)
    assert stats.delivered == 1
    assert delivered == [event_id, event_id]
    assert await _published_at(relay_sm, tenant, event_id) is not None

    # 再跑一轮：已投递事件不再被领取（重复上限是"崩溃次数"，不是无限）。
    stats_again = await relay.drain_tenant(tenant)
    assert stats_again.delivered == 0
    assert delivered == [event_id, event_id]

    record(
        "outbox_crash_between_handler_and_ack",
        handler_deliveries=len(delivered),
        distinct_events=len(set(delivered)),
        published_exactly_once=True,
        redelivery_is_expected=True,
        invariant="no loss; duplicate delivery bounded by crashes",
    )
