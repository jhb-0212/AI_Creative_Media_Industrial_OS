"""审批引擎领域层单测：AND/OR 判定与票据终态不变量（纯内存，无数据库）。

验证三条结构性性质（ADR-R11-G）：

1. **模式语义**：ALL 每组至少一票赞成才通过、任一否决立即驳回；
   ANY 任一赞成即通过、全部否决才驳回。同组多人不叠加权重。
2. **结论是纯函数**：同样输入必得同样输出，不依赖时钟或仓储。
3. **领域红线**：Agent 不得作为投票人；否决必须有理由；终态票据必须有结论时间。
"""

from __future__ import annotations

import uuid

import pytest

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.identity.principal import Principal, SubjectType
from services.workflow.domain.approval import (
    ApprovalGroup,
    ApprovalStep,
    ApprovalTicket,
    evaluate,
)
from services.workflow.domain.enums import ApprovalMode, TicketStatus, VoteDecision
from services.workflow.domain.state_machine import assert_transition

_TENANT = uuid.UUID("00000000-0000-0000-0000-00000000d5a1")
_GROUPS = (
    ApprovalGroup("delivery_owner", ("交付负责人",)),
    ApprovalGroup("qc_rights", ("QC Worker", "版权")),
)


def _principal(subject: str, *roles: str) -> Principal:
    return Principal(
        subject_id=subject,
        tenant_id=_TENANT,
        organization_id=_TENANT,
        roles=frozenset(roles),
        project_ids=frozenset({"*"}),
    )


def _step(
    group: str,
    decision: VoteDecision,
    *,
    voter: str = "voter-1",
    reason: str = "同意",
) -> ApprovalStep:
    return ApprovalStep(
        entity_id=f"step-{voter}-{group}",
        ticket_id="ticket-1",
        group_name=group,
        voter_subject_id=voter,
        voter_subject_type=SubjectType.HUMAN,
        decision=decision,
        reason=reason,
        tenant_id=_TENANT,
        organization_id=_TENANT,
        project_id="proj-approval-domain",
        created_by=voter,
    )


# --- AND（ALL）语义 -----------------------------------------------------------


def test_all_mode_needs_every_group_to_approve() -> None:
    """ALL：只有一组赞成 → 未完成；两组都赞成 → 通过。"""
    one = evaluate(
        ApprovalMode.ALL,
        _GROUPS,
        (_step("delivery_owner", VoteDecision.APPROVE),),
    )
    assert one is None

    both = evaluate(
        ApprovalMode.ALL,
        _GROUPS,
        (
            _step("delivery_owner", VoteDecision.APPROVE),
            _step("qc_rights", VoteDecision.APPROVE, voter="voter-2"),
        ),
    )
    assert both is TicketStatus.APPROVED


def test_all_mode_any_reject_fails_immediately() -> None:
    """ALL：任一分组出现否决，即使其它组全赞成也立即驳回。"""
    result = evaluate(
        ApprovalMode.ALL,
        _GROUPS,
        (
            _step("delivery_owner", VoteDecision.APPROVE),
            _step("qc_rights", VoteDecision.REJECT, reason="权利证据不全"),
        ),
    )
    assert result is TicketStatus.REJECTED


def test_all_mode_same_group_extra_votes_do_not_add_weight() -> None:
    """同组多人投票不叠加权重：一组一票权，组内 OR。"""
    # delivery_owner 组两人赞成，qc_rights 组无人 → 仍不能通过。
    result = evaluate(
        ApprovalMode.ALL,
        _GROUPS,
        (
            _step("delivery_owner", VoteDecision.APPROVE, voter="voter-1"),
            _step("delivery_owner", VoteDecision.APPROVE, voter="voter-2"),
        ),
    )
    assert result is None


# --- OR（ANY）语义 ------------------------------------------------------------


def test_any_mode_one_approve_passes() -> None:
    """ANY：任一分组赞成即通过。"""
    result = evaluate(
        ApprovalMode.ANY,
        _GROUPS,
        (_step("qc_rights", VoteDecision.APPROVE),),
    )
    assert result is TicketStatus.APPROVED


def test_any_mode_needs_all_groups_reject_to_fail() -> None:
    """ANY：只有全部分组都否决才驳回；部分否决仍是未完成。"""
    partial = evaluate(
        ApprovalMode.ANY,
        _GROUPS,
        (_step("delivery_owner", VoteDecision.REJECT, reason="不合格"),),
    )
    assert partial is None

    all_reject = evaluate(
        ApprovalMode.ANY,
        _GROUPS,
        (
            _step("delivery_owner", VoteDecision.REJECT, reason="不合格"),
            _step("qc_rights", VoteDecision.REJECT, reason="权利风险", voter="voter-2"),
        ),
    )
    assert all_reject is TicketStatus.REJECTED


def test_evaluate_is_pure_no_votes_returns_none() -> None:
    """无选票时不臆测结论。"""
    assert evaluate(ApprovalMode.ALL, _GROUPS, ()) is None
    assert evaluate(ApprovalMode.ANY, _GROUPS, ()) is None


# --- 分组资格与领域红线 ------------------------------------------------------


def test_group_accepts_roles_but_rejects_wrong_group() -> None:
    """分组资格：角色交集判定，组内角色为 OR。"""
    assert _GROUPS[0].accepts(_principal("owner", "交付负责人"))
    assert not _GROUPS[0].accepts(_principal("qc", "QC Worker"))
    assert _GROUPS[1].accepts(_principal("qc", "QC Worker"))
    assert _GROUPS[1].accepts(_principal("rights", "版权"))


def test_agent_cannot_be_voter() -> None:
    """Agent 不得投票：领域层第二道防线（授权层与数据库还有两道）。"""
    with pytest.raises(DomainError) as exc:
        ApprovalStep(
            entity_id="step-agent",
            ticket_id="ticket-1",
            group_name="delivery_owner",
            voter_subject_id="agent-1",
            voter_subject_type=SubjectType.AGENT,
            decision=VoteDecision.APPROVE,
            reason="同意",
            tenant_id=_TENANT,
            organization_id=_TENANT,
            project_id="proj-approval-domain",
        )
    assert exc.value.error_code is ErrorRegistry.AUTH_SCOPE_DENIED


def test_reject_requires_reason() -> None:
    """否决必须写明理由（数据库 CHECK 之外还有领域校验）。"""
    with pytest.raises(DomainError) as exc:
        _step("qc_rights", VoteDecision.REJECT, reason="  ")
    assert exc.value.error_code is ErrorRegistry.VALIDATION_ERROR


# --- 状态机与终态不变量 ------------------------------------------------------


@pytest.mark.parametrize("terminal", [TicketStatus.APPROVED, TicketStatus.REJECTED])
def test_terminal_ticket_cannot_revive(terminal: TicketStatus) -> None:
    """终态票据不可原地复活：没有出边。"""
    with pytest.raises(DomainError) as exc:
        assert_transition(terminal, TicketStatus.PENDING)
    assert exc.value.error_code is ErrorRegistry.STATE_TRANSITION_DENIED


def test_pending_can_go_to_approved_or_rejected() -> None:
    assert_transition(TicketStatus.PENDING, TicketStatus.APPROVED)
    assert_transition(TicketStatus.PENDING, TicketStatus.REJECTED)


def test_terminal_ticket_requires_completed_at() -> None:
    """终态票据必须有结论时间；缺失时构造即拒绝。"""
    with pytest.raises(DomainError) as exc:
        ApprovalTicket(
            entity_id="ticket-1",
            flow_id="flow-1",
            project_id="proj-approval-domain",
            tenant_id=_TENANT,
            organization_id=_TENANT,
            status=TicketStatus.APPROVED,
            completed_at=None,
        )
    assert exc.value.error_code is ErrorRegistry.VALIDATION_ERROR
