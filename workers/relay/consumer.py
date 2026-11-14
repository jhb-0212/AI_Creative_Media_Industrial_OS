"""Outbox 事件投递消费端（R06）——按租户循环消费，RLS 始终生效。

决策依据：docs/adr/ADR-R06-outbox-relay-tenancy.md（选方案 B）。
不选方案 A（专用 BYPASSRLS 角色）的唯一原因是：那会把「跨租户读」这个已经
堵在数据库层的洞重新打开——relay 一旦被误用/攻破就是全租户审计负载泄露。

关键语义（每一条都是刻意的，不是实现细节）：
1. 连接角色就是 app_role（与 API 相同）。relay 没有任何特权，越界由 RLS 拒绝。
2. 跨租户能力只有一处：``outbox.list_active_tenants()``（SECURITY DEFINER，
   只返回 tenant_id 与积压统计，不返回 payload）。
3. 租户内一个事务：``SET LOCAL app.current_tenant_id`` → ``SELECT ... FOR UPDATE
   SKIP LOCKED`` → 处理器 → 回写 ``published_at`` → commit。租户内按
   ``occurred_at, event_id`` 有序；跨租户无序（审计投递不需要全局序）。
4. **至少一次**：处理器成功与 ack 同事务提交；进程在「处理器已执行、事务未提交」
   之间崩溃会重投。消费方必须按 ``event_id`` 幂等——这是 outbox 的固有语义，
   不是缺陷。
5. 没有注册处理器的事件 **不 ack**：宁可让积压涨到报警，也不谎报「已投递」。
   谎报的代价是事件永久丢失且无人知晓。
6. 单条失败不阻塞同批其余事件，也不阻塞其他租户；失败事件保持未投递，下一轮重试。
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.shared.logging import get_logger
from packages.shared.metrics import (
    OUTBOX_RELAY_BACKLOG,
    OUTBOX_RELAY_EVENTS_TOTAL,
    OUTBOX_RELAY_RUN_ERRORS,
)
from packages.shared.rls import set_tenant_context

logger = get_logger(__name__)

SessionMaker = async_sessionmaker[AsyncSession]


@dataclass(frozen=True)
class OutboxEvent:
    """一条待投递事件（只读视图，字段与 outbox.outbox_events 对齐）。"""

    event_id: uuid.UUID
    aggregate_type: str
    aggregate_id: str
    tenant_id: uuid.UUID
    event_type: str
    payload: dict[str, Any]
    occurred_at: datetime


EventHandler = Callable[[OutboxEvent], Awaitable[None]]


@dataclass
class RelayStats:
    """单轮投递统计（供日志与测试断言）。"""

    tenants_scanned: int = 0
    delivered: int = 0
    unrouted: int = 0
    failed: int = 0


# 枚举有积压的租户（唯一的跨租户入口，只返回元数据）。
_LIST_TENANTS = text(
    "SELECT tenant_id, pending_count "
    "FROM outbox.list_active_tenants() "
    "ORDER BY pending_count DESC, tenant_id"
)

# 本租户内领取待投递事件。FOR UPDATE + SKIP LOCKED：多副本 relay 并行时各自
# 领到不同事件，不会重复投递，也不会互相阻塞。
_CLAIM = text(
    """
    SELECT event_id, aggregate_type, aggregate_id, tenant_id,
           event_type, payload, occurred_at
    FROM outbox.outbox_events
    WHERE published_at IS NULL
    ORDER BY occurred_at, event_id
    LIMIT :batch
    FOR UPDATE SKIP LOCKED
    """
)

# ack 带 published_at IS NULL 前置条件：重复 ack 不会覆盖首次投递时间。
_ACK = text(
    "UPDATE outbox.outbox_events SET published_at = :now "
    "WHERE event_id = :event_id AND published_at IS NULL"
)


class OutboxRelay:
    """按租户循环投递 outbox 事件。"""

    def __init__(
        self,
        sessionmaker: SessionMaker,
        handlers: Mapping[str, EventHandler] | None = None,
        *,
        batch_size: int = 100,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._handlers: dict[str, EventHandler] = dict(handlers or {})
        self._batch_size = batch_size

    async def run_once(self, *, batch_size: int | None = None) -> RelayStats:
        """扫描一轮：列出有积压的租户，逐租户投递一批事件。

        不做无限循环、不 sleep：调度节奏由调用方（``__main__`` 或测试）决定，
        这样单轮行为可被单测直接断言。
        """
        stats = RelayStats()
        limit = batch_size or self._batch_size

        async with self._sessionmaker() as session:
            try:
                tenants = (
                    await session.execute(_LIST_TENANTS)
                ).mappings().all()
            except Exception:
                # 枚举失败（函数缺失/权限收回）必须显式暴露，不能静默空转。
                OUTBOX_RELAY_RUN_ERRORS.labels(stage="enumerate").inc()
                logger.exception("outbox_relay_enumerate_failed")
                raise

        OUTBOX_RELAY_BACKLOG.set(sum(int(row["pending_count"]) for row in tenants))

        for row in tenants:
            stats.tenants_scanned += 1
            try:
                await self.drain_tenant(
                    uuid.UUID(str(row["tenant_id"])),
                    batch_size=limit,
                    stats=stats,
                )
            except Exception:
                # 单租户失败不影响其他租户：租户之间没有共享事务，隔离在此处体现。
                OUTBOX_RELAY_RUN_ERRORS.labels(stage="tenant").inc()
                logger.exception(
                    "outbox_relay_tenant_failed", tenant_id=str(row["tenant_id"])
                )

        if stats.delivered or stats.unrouted or stats.failed:
            logger.info(
                "outbox_relay_round",
                tenants=stats.tenants_scanned,
                delivered=stats.delivered,
                unrouted=stats.unrouted,
                failed=stats.failed,
            )
        return stats

    async def drain_tenant(
        self,
        tenant_id: uuid.UUID,
        *,
        batch_size: int | None = None,
        stats: RelayStats | None = None,
    ) -> RelayStats:
        """在指定租户上下文内投递一批事件（一个事务）。

        公开而非私有：单租户排空是 relay 的真实能力（重放某租户、灰度某租户、
        测试隔离语义都靠它），不是仅供测试的钩子。
        """
        stats = stats or RelayStats()
        limit = batch_size or self._batch_size
        async with self._sessionmaker() as session:
            await set_tenant_context(session, tenant_id)
            rows = (await session.execute(_CLAIM, {"batch": limit})).mappings().all()
            for row in rows:
                event = OutboxEvent(
                    event_id=uuid.UUID(str(row["event_id"])),
                    aggregate_type=row["aggregate_type"],
                    aggregate_id=row["aggregate_id"],
                    tenant_id=uuid.UUID(str(row["tenant_id"])),
                    event_type=row["event_type"],
                    payload=row["payload"],
                    occurred_at=row["occurred_at"],
                )
                handler = self._handlers.get(event.event_type)
                if handler is None:
                    # 没有下游：保持未投递（不 ack）。积压是可见的诚实状态。
                    stats.unrouted += 1
                    OUTBOX_RELAY_EVENTS_TOTAL.labels(
                        event_type=event.event_type, result="unrouted"
                    ).inc()
                    continue
                try:
                    await handler(event)
                except Exception:
                    stats.failed += 1
                    OUTBOX_RELAY_EVENTS_TOTAL.labels(
                        event_type=event.event_type, result="failed"
                    ).inc()
                    logger.exception(
                        "outbox_relay_handler_failed",
                        event_id=str(event.event_id),
                        event_type=event.event_type,
                    )
                    continue
                await session.execute(
                    _ACK,
                    {"now": datetime.now(UTC), "event_id": event.event_id},
                )
                stats.delivered += 1
                OUTBOX_RELAY_EVENTS_TOTAL.labels(
                    event_type=event.event_type, result="delivered"
                ).inc()
            await session.commit()
        return stats
