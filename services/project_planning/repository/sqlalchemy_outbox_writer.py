"""SQLAlchemy 实现的 Outbox 写入端口（绑定当前工作单元会话）。"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from packages.shared.outbox_writer import record_event
from services.project_planning.repository.outbox_writer import OutboxWriter


class SqlAlchemyOutboxWriter(OutboxWriter):
    """基于 SQLAlchemy 会话的审计事件写入实现。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def record(
        self,
        *,
        aggregate_type: str,
        aggregate_id: str,
        tenant_id: UUID,
        event_type: str,
        payload: dict[str, Any],
    ) -> UUID:
        # 仅登记（add），不 commit：与业务状态变更共享同一请求级事务。
        return record_event(
            self._session,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            tenant_id=tenant_id,
            event_type=event_type,
            payload=payload,
        )
