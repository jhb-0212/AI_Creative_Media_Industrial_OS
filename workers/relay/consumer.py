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
6. 单条失败不阻塞同批其余事件，也不阻塞其他租户；失败事件保持未投递，
   按指数退避重试；超过 `max_attempts` 进入死信（dead_lettered_at），不再领取。
   死信不删除：它是"投递放弃"的事实，必须可查、可人工重放（P2-2）。
7. **「没外发」不等于「失败一次」**（审查 N2）：处理器抛
   ``DeliveryDeferredError`` 时表示它明确知道本次没有发出请求（例如端点熔断
   拦截）。这类事件不计 attempts、不写退避、不推进死信——否则会被一串「根本
   没试过」的轮次推成死信，直接违背 ADR-R13「事件不丢弃」。端点级拦截对整批
   同样成立，因此本轮对该租户**立即停止**（不必逐条撞同一个闸门）。
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
    OUTBOX_RELAY_DEAD,
    OUTBOX_RELAY_EVENTS_TOTAL,
    OUTBOX_RELAY_RUN_ERRORS,
)
from packages.shared.rls import set_tenant_context
from workers.relay.errors import DeliveryDeferredError

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
    dead_lettered: int = 0
    # 本轮「未真正外发」而被推迟的事件数（熔断拦截等）。不计入 failed：
    # 它既不是成功也不是一次失败尝试，混进 failed 会让退避/死信口径失真。
    deferred: int = 0


# 枚举有未投递事件的租户（唯一的跨租户入口，只返回元数据）。
# pending_count 与 _CLAIM 同口径（可领取）；dead_count 单独统计死信。
_LIST_TENANTS = text(
    "SELECT tenant_id, pending_count, dead_count "
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
      AND dead_lettered_at IS NULL
      AND next_attempt_at <= now()
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

# 失败重试：attempts + 1，按指数退避（10s/20s/40s... 上限 640s），
# 达到 max_attempts 时写 dead_lettered_at，此后不再被 CLAIM 领取。
# 死信不删除——"投递放弃"本身是可审计事实，留待人工重放。
_FAIL = text(
    """
    UPDATE outbox.outbox_events
    SET attempts = attempts + 1,
        next_attempt_at = now()
            + (interval '10 seconds' * power(2, least(attempts, 6))),
        dead_lettered_at = CASE
            WHEN attempts + 1 >= :max_attempts THEN now()
            ELSE NULL
        END
    WHERE event_id = :event_id
    RETURNING attempts, dead_lettered_at
    """
)


class OutboxRelay:
    """按租户循环投递 outbox 事件。"""

    def __init__(
        self,
        sessionmaker: SessionMaker,
        handlers: Mapping[str, EventHandler] | None = None,
        *,
        batch_size: int = 100,
        default_handler: EventHandler | None = None,
        max_attempts: int = 5,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._handlers: dict[str, EventHandler] = dict(handlers or {})
        # 兜底处理器：未按 event_type 单独注册时用它（例如统一外发 webhook）。
        # 没有专用处理器也没有兜底 = 事件没有下游，保持未投递。
        self._default_handler = default_handler
        self._batch_size = batch_size
        if max_attempts < 1:
            raise ValueError("max_attempts 必须 >= 1")
        self._max_attempts = max_attempts

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

        # 积压与死信分离观测：backlog 只计「可领取」事件，死信不再虚高积压
        # 报警（复核 R2）。纯死信租户（pending_count == 0）不进 drain，避免每轮
        # 白扫一次 claim；死信仍通过 OUTBOX_RELAY_DEAD 保持可见。
        OUTBOX_RELAY_BACKLOG.set(sum(int(row["pending_count"]) for row in tenants))
        OUTBOX_RELAY_DEAD.set(sum(int(row["dead_count"]) for row in tenants))

        for row in tenants:
            if int(row["pending_count"]) == 0:
                # 无「可领取」事件：只有死信/退避中的事件，drain 领不到任何东西。
                continue
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

        if stats.delivered or stats.unrouted or stats.failed or stats.deferred:
            logger.info(
                "outbox_relay_round",
                tenants=stats.tenants_scanned,
                delivered=stats.delivered,
                unrouted=stats.unrouted,
                failed=stats.failed,
                dead_lettered=stats.dead_lettered,
                deferred=stats.deferred,
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
                handler = self._handlers.get(event.event_type) or self._default_handler
                if handler is None:
                    # 没有下游：保持未投递（不 ack）。积压是可见的诚实状态。
                    stats.unrouted += 1
                    OUTBOX_RELAY_EVENTS_TOTAL.labels(
                        event_type=event.event_type, result="unrouted"
                    ).inc()
                    continue
                try:
                    await handler(event)
                except DeliveryDeferredError as exc:
                    # 明确知道「没发出去」：这是推迟，不是一次失败尝试。
                    # 不递增 attempts、不写退避、不推进死信，事件保持未投递。
                    stats.deferred += 1
                    OUTBOX_RELAY_EVENTS_TOTAL.labels(
                        event_type=event.event_type, result="deferred"
                    ).inc()
                    logger.warning(
                        "outbox_relay_delivery_deferred",
                        event_id=str(event.event_id),
                        event_type=event.event_type,
                        reason=str(exc),
                    )
                    # 端点级闸门（如熔断 OPEN）对同批其余事件同样成立，本轮对该
                    # 租户立即停止，不再逐条撞同一个闸门；事务里没有写操作，
                    # 提交即释放行锁，下一轮重新领取。
                    break
                except Exception:
                    stats.failed += 1
                    OUTBOX_RELAY_EVENTS_TOTAL.labels(
                        event_type=event.event_type, result="failed"
                    ).inc()
                    attempts, dead_at = (
                        await session.execute(
                            _FAIL,
                            {
                                "event_id": event.event_id,
                                "max_attempts": self._max_attempts,
                            },
                        )
                    ).one()
                    logger.exception(
                        "outbox_relay_handler_failed",
                        event_id=str(event.event_id),
                        event_type=event.event_type,
                        attempts=int(attempts),
                    )
                    if dead_at is not None:
                        stats.dead_lettered += 1
                        OUTBOX_RELAY_EVENTS_TOTAL.labels(
                            event_type=event.event_type, result="dead"
                        ).inc()
                        logger.error(
                            "outbox_relay_dead_lettered",
                            event_id=str(event.event_id),
                            event_type=event.event_type,
                            attempts=int(attempts),
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
