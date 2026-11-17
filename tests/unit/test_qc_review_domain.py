"""QCReviewSession 领域规则单测（C5 的规则面）。

这里不碰数据库也不碰 HTTP：只验证"状态链、必填项、红线、追加式结论"这几条
不依赖基础设施的判定。跨层的证据（并发、数据库守卫、事件）在
``tests/integration/test_qc_review_api.py`` 与 ``test_qc_review_parity.py``。
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.identity.principal import Principal, SubjectType
from services.qc_governance.domain.enums import (
    QCIssueHumanDecision,
    QCIssueSeverity,
    QCReviewSessionStatus,
)
from services.qc_governance.domain.qc_issue import QCIssue
from services.qc_governance.domain.qc_review_session import (
    QCReviewSession,
    ReviewDecision,
)
from services.qc_governance.domain.state_machine import (
    QC_REVIEW_SESSION_ALLOWED_TRANSITIONS,
    allowed_targets,
    can_transition_review_session,
)

_TENANT = uuid.UUID("00000000-0000-0000-0000-0000000000c5")
_PROJECT = "proj_qc_domain"


def _principal(
    subject: str = "qc_reviewer", *, subject_type: SubjectType = SubjectType.HUMAN
) -> Principal:
    return Principal(
        subject_id=subject,
        tenant_id=_TENANT,
        organization_id=_TENANT,
        subject_type=subject_type,
        project_ids=frozenset({"*"}),
    )


def _issue(
    issue_id: str = "issue_1",
    *,
    severity: QCIssueSeverity = QCIssueSeverity.MAJOR,
    human_decision: QCIssueHumanDecision = QCIssueHumanDecision.PENDING,
    **overrides: Any,
) -> QCIssue:
    values: dict[str, Any] = {
        "issue_id": issue_id,
        "shot_id": "SHOT_001",
        "asset_version": 1,
        "timecode": "00:00:03:12",
        "frame_range": "84-96",
        "qc_dimension": "VISUAL",
        "severity": severity,
        "evidence": "frame_0087.png",
        "detector_version": 3,
        "human_decision": human_decision,
        "disposition": "PENDING",
    }
    values.update(overrides)
    return QCIssue(**values)


def _session(*issues: QCIssue, reviewers: list[str] | None = None) -> QCReviewSession:
    return QCReviewSession(
        entity_id="qcs_unit_001",
        project_id=_PROJECT,
        asset_version_id="av_unit_001",
        qc_report_ref="qcr_unit_001",
        reviewers=reviewers or ["qc_reviewer", "director_001"],
        findings=list(issues) or [_issue()],
        tenant_id=_TENANT,
        created_by="director_001",
    )


def _decision(
    issue: QCIssue,
    decision: QCIssueHumanDecision,
    *,
    principal: Principal | None = None,
    **kwargs: Any,
) -> ReviewDecision:
    return ReviewDecision.record(
        issue=issue,
        human_decision=decision,
        disposition="FIX_IN_NEXT_ITERATION",
        principal=principal or _principal(),
        **kwargs,
    )


class TestStateMachine:
    """状态链必须逐字等于 C.7（含裁决 ④ 的禁止跳关）。"""

    def test_transition_table_is_the_contract_chain(self) -> None:
        edges = {
            (src.value, tgt.value)
            for src, targets in QC_REVIEW_SESSION_ALLOWED_TRANSITIONS.items()
            for tgt in targets
        }
        assert edges == {
            ("OPEN", "IN_REVIEW"),
            ("IN_REVIEW", "DECISION_PENDING"),
            ("DECISION_PENDING", "RESOLVED"),
            ("RESOLVED", "GATE_ELIGIBLE"),
            ("GATE_ELIGIBLE", "CLOSED"),
        }

    def test_resolved_to_closed_is_not_registered(self) -> None:
        assert not can_transition_review_session(
            QCReviewSessionStatus.RESOLVED, QCReviewSessionStatus.CLOSED
        )
        assert allowed_targets(QCReviewSessionStatus.RESOLVED) == frozenset(
            {QCReviewSessionStatus.GATE_ELIGIBLE}
        )

    def test_waive_with_authority_is_representable_but_not_enterable(self) -> None:
        """enum_registry 把它登记为状态，C.7 与 21.1 都当它结论类型（ADR-R36）。"""
        assert QCReviewSessionStatus.WAIVE_WITH_AUTHORITY.value == (
            "WAIVE_WITH_AUTHORITY"
        )
        for source in QCReviewSessionStatus:
            assert not can_transition_review_session(
                source, QCReviewSessionStatus.WAIVE_WITH_AUTHORITY
            )

    def test_closed_is_terminal(self) -> None:
        assert allowed_targets(QCReviewSessionStatus.CLOSED) == frozenset()


class TestQCIssueValidation:
    """21.1 的九项必须逐条成立：缺一即不是可复核的缺陷。"""

    def test_missing_required_field_is_rejected_with_field_name(self) -> None:
        raw = _issue().to_dict()
        raw.pop("evidence")
        with pytest.raises(DomainError) as exc_info:
            QCIssue.from_mapping(raw, index=2)
        err = exc_info.value
        assert err.error_code is ErrorRegistry.VALIDATION_ERROR
        assert err.context["issue_index"] == 2
        assert err.context["missing_fields"] == ["evidence"]

    def test_blank_required_field_is_rejected(self) -> None:
        raw = _issue().to_dict()
        raw["shot_id"] = "   "
        with pytest.raises(DomainError) as exc_info:
            QCIssue.from_mapping(raw, index=0)
        assert "shot_id" in str(exc_info.value.context["missing_fields"])

    def test_unknown_severity_is_rejected_with_allowed_values(self) -> None:
        raw = _issue().to_dict()
        raw["severity"] = "BLOCKER"  # 7.6 的 ReworkTask 级别，不是缺陷级别
        with pytest.raises(DomainError) as exc_info:
            QCIssue.from_mapping(raw, index=0)
        assert "CRITICAL" in exc_info.value.detail

    def test_detector_extra_fields_are_preserved(self) -> None:
        raw = _issue().to_dict()
        raw["confidence"] = 0.72
        issue = QCIssue.from_mapping(raw, index=0)
        assert issue.to_dict()["confidence"] == 0.72

    def test_red_line_issue_flag(self) -> None:
        assert _issue(severity=QCIssueSeverity.CRITICAL).is_red_line
        assert not _issue(severity=QCIssueSeverity.MINOR).is_red_line


class TestReviewDecision:
    def test_pending_is_not_a_decision(self) -> None:
        with pytest.raises(DomainError) as exc_info:
            _decision(_issue(), QCIssueHumanDecision.PENDING)
        assert "PENDING" in exc_info.value.detail

    def test_waiver_requires_authority_and_reason(self) -> None:
        with pytest.raises(DomainError) as exc_info:
            _decision(_issue(), QCIssueHumanDecision.WAIVE_WITH_AUTHORITY)
        assert "授权主体与理由" in exc_info.value.detail

    def test_red_line_cannot_be_waived(self) -> None:
        issue = _issue(severity=QCIssueSeverity.CRITICAL, qc_dimension="RIGHTS")
        with pytest.raises(DomainError) as exc_info:
            _decision(
                issue,
                QCIssueHumanDecision.WAIVE_WITH_AUTHORITY,
                waiver_authority="director_001",
                waiver_reason="艺术表达需要",
            )
        assert "不得以任何理由豁免" in exc_info.value.detail

    def test_non_critical_waiver_is_allowed_with_authority(self) -> None:
        decision = _decision(
            _issue(severity=QCIssueSeverity.MINOR),
            QCIssueHumanDecision.WAIVE_WITH_AUTHORITY,
            waiver_authority="director_001",
            waiver_reason="艺术表达需要",
        )
        assert decision.waiver_authority == "director_001"

    def test_agent_cannot_decide(self) -> None:
        """21.2：AI 只发现与提供证据，不单独裁决。"""
        with pytest.raises(DomainError) as exc_info:
            _decision(
                _issue(),
                QCIssueHumanDecision.FALSE_POSITIVE,
                principal=_principal("qc_agent", subject_type=SubjectType.AGENT),
            )
        assert exc_info.value.error_code is ErrorRegistry.AUTH_SCOPE_DENIED


class TestSessionLifecycle:
    def test_empty_session_is_rejected(self) -> None:
        with pytest.raises(DomainError):
            QCReviewSession(
                entity_id="qcs_empty",
                project_id=_PROJECT,
                asset_version_id="av_1",
                qc_report_ref="qcr_1",
                reviewers=["a"],
                findings=[],
                tenant_id=_TENANT,
                created_by="a",
            )

    def test_duplicate_issue_ids_are_rejected(self) -> None:
        with pytest.raises(DomainError):
            _session(_issue("i1"), _issue("i1"))

    def test_decision_pending_requires_every_issue_decided(self) -> None:
        session = _session(_issue("i1"), _issue("i2"))
        session.start_review(principal=_principal())
        session.record_decisions([_decision(_issue("i1"), QCIssueHumanDecision.CONFIRM_DEFECT)])
        with pytest.raises(DomainError) as exc_info:
            session.advance(
                QCReviewSessionStatus.DECISION_PENDING, principal=_principal()
            )
        assert exc_info.value.context["undecided_issue_ids"] == ["i2"]

    def test_resolved_requires_no_unresolved_evidence(self) -> None:
        session = _session(_issue("i1"))
        session.start_review(principal=_principal())
        session.record_decisions(
            [_decision(_issue("i1"), QCIssueHumanDecision.NEEDS_MORE_EVIDENCE)]
        )
        session.advance(QCReviewSessionStatus.DECISION_PENDING, principal=_principal())
        with pytest.raises(DomainError) as exc_info:
            session.advance(QCReviewSessionStatus.RESOLVED, principal=_principal())
        assert exc_info.value.context["unresolved_issue_ids"] == ["i1"]

    def test_full_chain_reaches_closed(self) -> None:
        issue = _issue("i1")
        session = _session(issue)
        reviewer = _principal("qc_reviewer")
        session.start_review(principal=reviewer)
        # 闭环结论用「误报」：已确认缺陷按 21.2 必须返工，不能进 RESOLVED
        # （见 TestDefectClosure）。这里要覆盖的是状态链，不是缺陷处置。
        session.record_decisions(
            [_decision(issue, QCIssueHumanDecision.FALSE_POSITIVE)]
        )
        for target in (
            QCReviewSessionStatus.DECISION_PENDING,
            QCReviewSessionStatus.RESOLVED,
            QCReviewSessionStatus.GATE_ELIGIBLE,
            QCReviewSessionStatus.CLOSED,
        ):
            session.advance(target, principal=reviewer)
        assert session.status is QCReviewSessionStatus.CLOSED
        # 1(新建) + start_review + record_decisions + 4 次推进 = 7
        assert session.entity_version == 7

    def test_jump_from_resolved_to_closed_is_denied(self) -> None:
        issue = _issue("i1")
        session = _session(issue)
        reviewer = _principal("qc_reviewer")
        session.start_review(principal=reviewer)
        session.record_decisions(
            [_decision(issue, QCIssueHumanDecision.FALSE_POSITIVE)]
        )
        session.advance(QCReviewSessionStatus.DECISION_PENDING, principal=reviewer)
        session.advance(QCReviewSessionStatus.RESOLVED, principal=reviewer)

        with pytest.raises(DomainError) as exc_info:
            session.advance(QCReviewSessionStatus.CLOSED, principal=reviewer)
        err = exc_info.value
        assert err.error_code is ErrorRegistry.STATE_TRANSITION_DENIED
        assert err.context["allowed_targets"] == ["GATE_ELIGIBLE"]
        assert session.status is QCReviewSessionStatus.RESOLVED

    def test_decisions_are_append_only_and_latest_wins(self) -> None:
        issue = _issue("i1")
        session = _session(issue)
        reviewer = _principal("qc_reviewer")
        session.start_review(principal=reviewer)
        session.record_decisions([_decision(issue, QCIssueHumanDecision.CONFIRM_DEFECT)])
        session.record_decisions([_decision(issue, QCIssueHumanDecision.FALSE_POSITIVE)])

        assert len(session.decisions) == 2  # 历史保留
        assert (
            session.latest_decisions()["i1"].human_decision
            is QCIssueHumanDecision.FALSE_POSITIVE
        )

    def test_resolved_session_rejects_new_decisions(self) -> None:
        issue = _issue("i1")
        session = _session(issue)
        reviewer = _principal("qc_reviewer")
        session.start_review(principal=reviewer)
        session.record_decisions([_decision(issue, QCIssueHumanDecision.FALSE_POSITIVE)])
        session.advance(QCReviewSessionStatus.DECISION_PENDING, principal=reviewer)
        session.advance(QCReviewSessionStatus.RESOLVED, principal=reviewer)

        with pytest.raises(DomainError) as exc_info:
            session.record_decisions(
                [_decision(issue, QCIssueHumanDecision.FALSE_POSITIVE)]
            )
        assert exc_info.value.error_code is ErrorRegistry.STATE_TRANSITION_DENIED

    def test_unknown_issue_decision_is_rejected(self) -> None:
        session = _session(_issue("i1"))
        session.start_review(principal=_principal())
        with pytest.raises(DomainError) as exc_info:
            session.record_decisions(
                [_decision(_issue("i9"), QCIssueHumanDecision.CONFIRM_DEFECT)]
            )
        assert "不在本次会话内" in exc_info.value.detail

    def test_advance_registers_the_actor_as_reviewer(self) -> None:
        session = _session(_issue("i1"), reviewers=["director_001"])
        session.start_review(principal=_principal("qc_reviewer"))
        assert "qc_reviewer" in session.reviewers


class TestDefectClosure:
    """已确认缺陷不等于已解决缺陷（21.2 / 4.3）。

    这组用例存在的原因是一个真实缺陷：``unresolved_issue_ids`` 曾只认
    ``NEEDS_MORE_EVIDENCE``，于是一条 CRITICAL 缺陷被判 CONFIRM_DEFECT 后，
    会话仍可 RESOLVED → GATE_ELIGIBLE → CLOSED——「确认即放行」。
    """

    def test_confirmed_defect_blocks_resolved(self) -> None:
        issue = _issue("i1", severity=QCIssueSeverity.MAJOR)
        session = _session(issue)
        reviewer = _principal("qc_reviewer")
        session.start_review(principal=reviewer)
        session.record_decisions(
            [_decision(issue, QCIssueHumanDecision.CONFIRM_DEFECT)]
        )
        session.advance(QCReviewSessionStatus.DECISION_PENDING, principal=reviewer)

        with pytest.raises(DomainError) as exc_info:
            session.advance(QCReviewSessionStatus.RESOLVED, principal=reviewer)
        err = exc_info.value
        assert err.error_code is ErrorRegistry.VALIDATION_ERROR
        assert err.context["pending_rework_issue_ids"] == ["i1"]
        assert err.context["unresolved_issue_ids"] == ["i1"]
        # 状态没有偷偷前进（失败不能留下半推进的会话）。
        assert session.status is QCReviewSessionStatus.DECISION_PENDING

    def test_red_line_confirmed_defect_blocks_resolved(self) -> None:
        issue = _issue("i1", severity=QCIssueSeverity.CRITICAL)
        session = _session(issue)
        reviewer = _principal("qc_reviewer")
        session.start_review(principal=reviewer)
        session.record_decisions(
            [_decision(issue, QCIssueHumanDecision.CONFIRM_DEFECT)]
        )
        session.advance(QCReviewSessionStatus.DECISION_PENDING, principal=reviewer)

        with pytest.raises(DomainError) as exc_info:
            session.advance(QCReviewSessionStatus.RESOLVED, principal=reviewer)
        assert exc_info.value.context["pending_rework_issue_ids"] == ["i1"]

    def test_redecision_to_false_positive_lifts_the_block(self) -> None:
        """改判是唯一出口：结论只追加、最新一条生效。

        返工完成后若复核认为原判是误报，可追加一条 FALSE_POSITIVE 解除阻断；
        这不是"绕过"，因为新结论带自己的 decided_by / decided_at 与理由。
        """
        issue = _issue("i1")
        session = _session(issue)
        reviewer = _principal("qc_reviewer")
        session.start_review(principal=reviewer)
        session.record_decisions(
            [_decision(issue, QCIssueHumanDecision.CONFIRM_DEFECT)]
        )
        session.advance(QCReviewSessionStatus.DECISION_PENDING, principal=reviewer)
        session.record_decisions(
            [_decision(issue, QCIssueHumanDecision.FALSE_POSITIVE)]
        )
        session.advance(QCReviewSessionStatus.RESOLVED, principal=reviewer)
        assert session.status is QCReviewSessionStatus.RESOLVED
        # 历史仍在：两条结论都在，最新一条是误报。
        assert len(session.decisions) == 2
        assert session.pending_rework_issue_ids() == []

    def test_only_closed_decisions_clear_an_issue(self) -> None:
        """只有误报/艺术性选择/授权豁免把缺陷移出未闭合集合。"""
        issues = [
            _issue("i1", human_decision=QCIssueHumanDecision.FALSE_POSITIVE),
            _issue("i2", human_decision=QCIssueHumanDecision.ARTISTIC_INTENTIONAL),
            _issue("i3", human_decision=QCIssueHumanDecision.NEEDS_MORE_EVIDENCE),
            _issue("i4", human_decision=QCIssueHumanDecision.CONFIRM_DEFECT),
        ]
        session = _session(*issues)
        reviewer = _principal("qc_reviewer")
        session.start_review(principal=reviewer)
        session.record_decisions(
            [
                _decision(issues[0], QCIssueHumanDecision.FALSE_POSITIVE),
                _decision(issues[1], QCIssueHumanDecision.ARTISTIC_INTENTIONAL),
                _decision(issues[2], QCIssueHumanDecision.NEEDS_MORE_EVIDENCE),
                _decision(issues[3], QCIssueHumanDecision.CONFIRM_DEFECT),
            ]
        )
        assert session.unresolved_issue_ids() == ["i3", "i4"]
        assert session.evidence_pending_issue_ids() == ["i3"]
        assert session.pending_rework_issue_ids() == ["i4"]
