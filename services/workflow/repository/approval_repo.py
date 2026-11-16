"""审批引擎仓储端口（事实 append-only，票据有乐观锁）。

端口刻意区分两种写语义：
- ``add_flow`` / ``add_step`` 没有 save：审批配置与选票是不可改写的事实；
- ``save_ticket`` 带期望版本：票据状态是投影，允许推进但不允许静默覆盖并发结果。
"""

from __future__ import annotations

from typing import Protocol

from services.workflow.domain.approval import ApprovalFlow, ApprovalStep, ApprovalTicket


class ApprovalRepository(Protocol):
    async def add_flow(self, flow: ApprovalFlow) -> None: ...

    async def get_flow(self, flow_id: str) -> ApprovalFlow | None: ...

    async def add_ticket(self, ticket: ApprovalTicket) -> None: ...

    async def get_ticket(self, ticket_id: str) -> ApprovalTicket | None: ...

    async def save_ticket(
        self, ticket: ApprovalTicket, *, expected_version: int
    ) -> None:
        """带乐观锁的票据状态推进；版本不匹配抛 ConcurrentModificationError。"""
        ...

    async def add_step(self, step: ApprovalStep) -> None:
        """追加一票；同一分组同一投票人重复提交抛 DuplicateEntityError。"""
        ...

    async def list_steps(self, ticket_id: str) -> tuple[ApprovalStep, ...]: ...

    async def find_step(
        self, *, ticket_id: str, group_name: str, voter_subject_id: str
    ) -> ApprovalStep | None: ...


__all__ = ["ApprovalRepository"]
