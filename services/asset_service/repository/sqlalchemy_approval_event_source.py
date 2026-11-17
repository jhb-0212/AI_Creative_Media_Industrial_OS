"""审批事件读取端口的 SQLAlchemy 实现（asset 域只读 outbox）。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.shared.outbox import OutboxEventModel
from services.asset_service.repository.approval_event_source import (
    ApprovalCompletedEvent,
    ApprovalEventSource,
)

_COMPLETED_EVENT_TYPE = "APPROVAL_TICKET_COMPLETED"


class SqlAlchemyApprovalEventSource(ApprovalEventSource):
    """从 outbox_events 读取审批完成事件（RLS 由会话 GUC 保证租户隔离）。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find_completed(
        self, *, action_key: str, resource_type: str, resource_id: str
    ) -> ApprovalCompletedEvent | None:
        stmt = (
            select(OutboxEventModel)
            .where(
                OutboxEventModel.event_type == _COMPLETED_EVENT_TYPE,
                OutboxEventModel.payload["action_key"].astext == action_key,
                OutboxEventModel.payload["resource_type"].astext == resource_type,
                OutboxEventModel.payload["resource_id"].astext == resource_id,
            )
            .order_by(OutboxEventModel.occurred_at.desc())
            .limit(1)
        )
        result = await self._session.execute(stmt)
        model = result.scalar_one_or_none()
        if model is None:
            return None
        return _to_event(model)

    async def list_completed(
        self, *, action_key: str, resource_type: str
    ) -> tuple[ApprovalCompletedEvent, ...]:
        stmt = (
            select(OutboxEventModel)
            .where(
                OutboxEventModel.event_type == _COMPLETED_EVENT_TYPE,
                OutboxEventModel.payload["action_key"].astext == action_key,
                OutboxEventModel.payload["resource_type"].astext == resource_type,
            )
            .order_by(OutboxEventModel.occurred_at.asc())
        )
        result = await self._session.execute(stmt)
        return tuple(_to_event(row) for row in result.scalars().all())


def _to_event(model: OutboxEventModel) -> ApprovalCompletedEvent:
    """ORM -> 领域事件视图。"""
    payload = model.payload
    project_id_raw = payload.get("project_id")
    return ApprovalCompletedEvent(
        event_id=model.event_id,
        action_key=str(payload.get("action_key", "")),
        resource_type=str(payload.get("resource_type", "")),
        resource_id=str(payload.get("resource_id", "")),
        decision=str(payload.get("decision", "")),
        votes=tuple(payload.get("votes", ())),
        occurred_at=model.occurred_at,
        tenant_id=model.tenant_id,
        project_id=str(project_id_raw) if project_id_raw is not None else None,
    )


__all__ = ["SqlAlchemyApprovalEventSource"]
