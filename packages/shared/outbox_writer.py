"""Transactional Outbox 写入辅助（领域无关，供各领域服务层调用）。

设计要点（消除重复机制 R4）：
- 各领域服务在状态迁移后，需「在同一事务内追加一条不可变审计事件」。
  若让每个服务手写 ``OutboxEventModel(...) + session.add(...)``，会散落
  重复构造逻辑；收敛为单一 ``record_event`` 函数。
- 不 commit：事务边界仍由请求层（get_session 依赖）统一控制，本函数仅
  ``session.add``，保证「业务状态变更 + 事件」在同一事务原子提交/回滚。
- 事件类型（event_type）对齐契约 action_registry.yaml 的 audit_event 字段
  （如 SCRIPT_IMPORTED / SCRIPT_LOCK_APPROVED / SCRIPT_SUPERSEDED）。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from packages.shared.outbox import OutboxEventModel


def record_event(
    session: AsyncSession,
    *,
    aggregate_type: str,
    aggregate_id: str,
    event_type: str,
    payload: dict[str, Any],
) -> uuid.UUID:
    """在当前事务内追加一条不可变审计事件（不 commit）。

    参数：
    - session：当前工作单元会话（与业务状态变更共享同一事务）。
    - aggregate_type：聚合根类型（Project/ScriptRevision/Episode）。
    - aggregate_id：聚合根 entity_id。
    - event_type：事件类型（对齐契约 audit_event，如 SCRIPT_LOCK_APPROVED）。
    - payload：事件负载（before/after 状态、操作主体、理由、关联标识等）。

    返回：新事件的 event_id（供调用方关联/测试断言）。
    """
    event_id = uuid.uuid4()
    session.add(
        OutboxEventModel(
            event_id=event_id,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            event_type=event_type,
            payload=payload,
            occurred_at=datetime.now(UTC),
            published_at=None,
        )
    )
    return event_id
