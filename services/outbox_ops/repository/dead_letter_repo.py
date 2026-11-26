"""Outbox 运维（死信查询与人工处置）——仓储层。

只做三件事：按 ``event_id`` 锁定事件行、清死信标记（条件更新）、写审计行；
另加一个死信集合的只读分页查询。判定不在这里：什么算「可重放」、游标合不合法
都是领域/边界规则，写在 service 里；本层只回答「数据库里现在是什么」。
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

_DEAD_LETTER_COLUMNS = (
    "event_id, event_type, tenant_id, attempts, dead_lettered_at, occurred_at"
)

# 死信列表分页用键集（keyset）而不是 OFFSET：
# 死信是「待人工处置的工作队列」，处置过程中行会不断从结果集里消失；
# OFFSET 分页会因此在翻页时漏行（前面少一条，后面的整体前移）。键集分页以
# 「上一页最后一条」为锚点，天然免疫这个漂移，也不必为深翻页扫过已丢弃的行。
#
# 排序键是 (dead_lettered_at DESC, event_id DESC)：同一轮重试可能让多条事件在
# 同一时刻进入死信（dead_lettered_at 相同），只用时间做锚点会跨页丢/重行，
# 因此必须再叠一个唯一列把顺序变成全序。
#
# 为什么写成两条语句而不是一条带 ``OR`` 的：
# 1. ``(:cursor_at IS NULL OR ...)`` 会让优化器在通用计划下放弃用
#    ``ix_outbox_dead_letter`` 做索引扫描——一个只为首页存在的分支，
#    把每一页都拖慢了；
# 2. 参数类型不用猜：无游标时不必给 timestamptz/uuid 传 NULL 再依赖 CAST。
_DEAD_LETTERS_FIRST_PAGE = text(
    f"""
    SELECT {_DEAD_LETTER_COLUMNS}
    FROM outbox.outbox_events
    WHERE tenant_id = :tenant_id
      AND dead_lettered_at IS NOT NULL
      AND (CAST(:event_type AS text) IS NULL
           OR event_type = CAST(:event_type AS text))
    ORDER BY dead_lettered_at DESC, event_id DESC
    LIMIT :fetch
    """
)

_DEAD_LETTERS_AFTER_CURSOR = text(
    f"""
    SELECT {_DEAD_LETTER_COLUMNS}
    FROM outbox.outbox_events
    WHERE tenant_id = :tenant_id
      AND dead_lettered_at IS NOT NULL
      AND (CAST(:event_type AS text) IS NULL
           OR event_type = CAST(:event_type AS text))
      AND (dead_lettered_at, event_id) < (:cursor_at, :cursor_event_id)
    ORDER BY dead_lettered_at DESC, event_id DESC
    LIMIT :fetch
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


@dataclass(frozen=True)
class DeadLetterSummaryRow:
    """死信列表里的一行：只含排障需要的列，不带 payload。

    刻意不返回 ``payload``：运维排障先要回答「有多少、哪些类型、什么时候进的
    死信」，payload 里有业务数据（含跨域引用），列表接口把它整批吐出去既无必要，
    也把「只看运维元数据」的权限边界悄悄放大成「可读业务事件内容」。
    """

    event_id: UUID
    event_type: str
    tenant_id: UUID
    attempts: int
    dead_lettered_at: datetime
    occurred_at: datetime


class SqlAlchemyDeadLetterRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_dead_letters(
        self,
        *,
        tenant_id: UUID,
        fetch: int,
        event_type: str | None = None,
        after: tuple[datetime, UUID] | None = None,
    ) -> list[DeadLetterSummaryRow]:
        """按 (dead_lettered_at, event_id) 倒序取死信，最多 ``fetch`` 行。

        ``fetch`` 是「取多少行」而不是「页大小」：上层故意多取一行来判断
        「后面还有没有」，避免为了知道有无下一页而多发一次空查询。
        """
        if after is None:
            params: dict[str, object] = {
                "tenant_id": tenant_id,
                "event_type": event_type,
                "fetch": fetch,
            }
            statement = _DEAD_LETTERS_FIRST_PAGE
        else:
            params = {
                "tenant_id": tenant_id,
                "event_type": event_type,
                "fetch": fetch,
                "cursor_at": after[0],
                "cursor_event_id": after[1],
            }
            statement = _DEAD_LETTERS_AFTER_CURSOR
        rows = (await self._session.execute(statement, params)).all()
        return [
            DeadLetterSummaryRow(
                event_id=row.event_id,
                event_type=row.event_type,
                tenant_id=row.tenant_id,
                attempts=row.attempts,
                dead_lettered_at=row.dead_lettered_at,
                occurred_at=row.occurred_at,
            )
            for row in rows
        ]

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
