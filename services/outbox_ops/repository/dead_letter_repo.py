"""Outbox 运维（死信人工处置）——仓储层。

只做三件事：按 ``event_id`` 锁定事件行、清死信标记（条件更新）、写审计行。
判定不在这里：什么算「可重放」是领域规则，写在 service 里；本层只回答
「数据库里现在是什么」。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from packages.shared.outbox import DeadLetterReplayModel

# 为什么用裸 SQL 而不是 ORM：``outbox_events`` 物理上是按 occurred_at 分区的表，
# 唯一约束包含分区键。ORM 映射以 event_id 作主键（单条事件语义），但
# ``session.get`` 会按主键生成 WHERE，绕过「先锁行再判定」的语义；
# 这里要的是显式的 ``FOR UPDATE`` 与显式的列集合。
_LOCK_FOR_REPLAY = text(
    """
    SELECT event_id, event_type, tenant_id, attempts,
           dead_lettered_at, published_at
    FROM outbox.outbox_events
    WHERE event_id = :event_id
    FOR UPDATE
    """
)

# 三列一起改，条件里带死信与未投递：并发重放同一行时，后到的事务在前者提交后
# 会看到 dead_lettered_at 已为 NULL，匹配不到行 → 上层判 409（D6-6）。
# 不改 published_at：投递时间只能由真正投递成功的 relay 写。
# 用 RETURNING 而不是数 rowcount：前者本身就是「这一行确实被改到了」的证据，
# 不需要再把结果对象当游标解释；同时它对分区表与普通表行为一致。
_CLEAR_DEAD_LETTER = text(
    """
    UPDATE outbox.outbox_events
    SET dead_lettered_at = NULL,
        next_attempt_at = now(),
        attempts = 0
    WHERE event_id = :event_id
      AND dead_lettered_at IS NOT NULL
      AND published_at IS NULL
    RETURNING event_id
    """
)


@dataclass(frozen=True)
class DeadLetterRow:
    """锁定到的死信行（数据库事实，不是请求体）。"""

    event_id: UUID
    event_type: str
    tenant_id: UUID
    attempts: int
    dead_lettered_at: datetime | None
    published_at: datetime | None


class SqlAlchemyDeadLetterRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def lock(self, event_id: UUID) -> DeadLetterRow | None:
        """锁定事件行；不存在或不在本租户（RLS 过滤）一律返回 None。"""
        row = (
            await self._session.execute(_LOCK_FOR_REPLAY, {"event_id": event_id})
        ).first()
        if row is None:
            return None
        return DeadLetterRow(
            event_id=row.event_id,
            event_type=row.event_type,
            tenant_id=row.tenant_id,
            attempts=row.attempts,
            dead_lettered_at=row.dead_lettered_at,
            published_at=row.published_at,
        )

    async def clear_dead_letter(self, event_id: UUID) -> bool:
        """条件清标记，返回是否真的由本次调用完成了状态迁移。"""
        cleared = (
            await self._session.execute(_CLEAR_DEAD_LETTER, {"event_id": event_id})
        ).first()
        return cleared is not None

    async def record(self, replay: DeadLetterReplayModel) -> None:
        self._session.add(replay)
