"""审批票据状态机（转移集合的单一事实源，供领域服务与契约闸门共用）。

终态不可原地复活：APPROVED / REJECTED 没有出边。
CANCELLED 可表示但当前无触发动作，显式登记在 state_machine_gaps.yaml。
"""

from __future__ import annotations

from services.workflow.domain.enums import TicketStatus

TICKET_ALLOWED_TRANSITIONS: dict[TicketStatus, frozenset[TicketStatus]] = {
    TicketStatus.PENDING: frozenset(
        {TicketStatus.APPROVED, TicketStatus.REJECTED}
    ),
    TicketStatus.APPROVED: frozenset(),
    TicketStatus.REJECTED: frozenset(),
    TicketStatus.CANCELLED: frozenset(),
}


def assert_transition(
    source: TicketStatus, target: TicketStatus
) -> None:
    """转移必须在表中登记；否则拒绝（终态不可原地复活）。"""
    if target not in TICKET_ALLOWED_TRANSITIONS[source]:
        from packages.errors.error_codes import DomainError, ErrorRegistry

        raise DomainError(
            ErrorRegistry.STATE_TRANSITION_DENIED,
            detail=f"审批票据非法转移 {source.value} -> {target.value}",
        )
