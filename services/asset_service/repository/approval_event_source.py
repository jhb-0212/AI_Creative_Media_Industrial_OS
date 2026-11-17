"""审批完成事件的只读端口（asset 域消费 outbox 权威事件）。

为什么是只读端口而不是直接查 outbox 表：
- 服务层不该知道 OutboxEventModel 的字段布局；端口把"我需要一条已完成的审批
  事件"这件事表达成领域可用的查询；
- asset 角色对 outbox_events 只有 SELECT/INSERT（无 UPDATE），
  消费端**不能**把事件标记成已读——幂等靠清单状态本身（见
  `DeliveryApprovalService`），这是 outbox「至少一次」语义的标准处置。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


@dataclass(frozen=True)
class ApprovalCompletedEvent:
    """一条已完成的审批事件视图（只取消费所需字段）。"""

    event_id: UUID
    action_key: str
    resource_type: str
    resource_id: str
    decision: str
    votes: tuple[dict[str, str], ...]
    occurred_at: datetime
    tenant_id: UUID
    #: 审批票据发起的项目归属（R4-2：消费端用它比对目标资源，拒绝跨项目推进）。
    #: 旧事件（升级前落库）可能没有该字段，因此允许为 None，消费端按 fail-closed
    #: 处理（缺失即拒绝推进，绝不假设"没有项目字段 = 全部项目"）。
    project_id: str | None = None


class ApprovalEventSource(ABC):
    """审批事件读取端口。"""

    @abstractmethod
    async def find_completed(
        self, *, action_key: str, resource_type: str, resource_id: str
    ) -> ApprovalCompletedEvent | None:
        """查一条匹配的 APPROVAL_TICKET_COMPLETED 事件；不存在返回 None。

        查询受当前会话的 RLS 上下文约束（租户隔离）；不匹配 decision 的事件
        由调用方判定——这里返回任意结论的完成事件，让用例层决定 APPROVED 才推进。
        """
        raise NotImplementedError

    @abstractmethod
    async def list_completed(
        self, *, action_key: str, resource_type: str
    ) -> tuple[ApprovalCompletedEvent, ...]:
        """列出所有匹配的完成事件（worker 批量扫描用，按时间升序）。"""
        raise NotImplementedError


__all__ = ["ApprovalCompletedEvent", "ApprovalEventSource"]
