"""QC 复查用例服务（QC.REVIEW.OPEN / QC.REVIEW.DECIDE）。

职责边界：
- 只做**本域**的事：登记会话、追加人工结论、按登记边推进状态、发审计事件。
- 跨域只读一件事：QC 报告是否存在、属于哪个项目/资产版本（经端口，ADR-R28）。
  这个校验不是"多余的一次查询"——没有它，会话可以引用一个不存在的报告，
  复核就变成了"凭空宣布有人看过证据"。

三条刻意未做的事（ADR-R36 §3 显式债务）：
1. 不派生 ReworkTask（21.2「对已确认缺陷创建修复任务」）：返工域尚未建模，
   在这里顺手写一张返工单等于凭空造对象与状态机。
2. 不判定 Gate 是否通过：``GATE_ELIGIBLE`` 只表示"证据齐备、可以进 Gate"，
   Gate 结论只能由 ``PRODUCTION.GATE.APPROVE`` 产生（单一裁决点，ADR-R23）。
3. 不做会签与冲突裁决：``reviewers`` 是参与人记录，不是多方签名。
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.errors.repository_errors import DuplicateEntityError
from packages.identity.principal import Principal
from packages.shared.outbox_writer import OutboxWriter
from services.qc_governance.domain.enums import (
    QCIssueHumanDecision,
    QCReviewSessionStatus,
)
from services.qc_governance.domain.ports import QCReportRefPort
from services.qc_governance.domain.qc_issue import QCIssue
from services.qc_governance.domain.qc_review_session import (
    ENTITY_TYPE,
    QCReviewSession,
    ReviewDecision,
)
from services.qc_governance.repository.qc_review_session_repo import (
    QCReviewSessionRepository,
)


class QCReviewService:
    """QC 复查会话用例（单域事务；提交点由路由控制）。"""

    def __init__(
        self,
        sessions: QCReviewSessionRepository,
        report_refs: QCReportRefPort,
        outbox: OutboxWriter,
    ) -> None:
        self._sessions = sessions
        self._report_refs = report_refs
        self._outbox = outbox

    # ---------------------------------------------------------------- 开启
    async def open_session(
        self,
        *,
        project_id: str,
        asset_version_id: str,
        qc_report_ref: str,
        reference_set: Mapping[str, Any],
        reviewers: Sequence[str],
        findings: Sequence[Mapping[str, Any]],
        principal: Principal,
        session_id: str | None = None,
    ) -> QCReviewSession:
        """登记一次复查会话（同一报告幂等：内容一致返回既有会话）。"""
        facts = await self._report_refs.resolve(
            tenant_id=principal.tenant_id,
            organization_id=principal.organization_id,
            qc_report_id=qc_report_ref,
        )
        if facts is None:
            # 不区分"不存在"与"不可见"：区分会让跨租户探测成为可能。
            raise DomainError(
                ErrorRegistry.NOT_FOUND,
                detail=f"QC 报告不存在或不在当前可见范围: {qc_report_ref}",
            )
        if facts.project_id != project_id:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail=(
                    f"QC 报告 {qc_report_ref} 属于项目 {facts.project_id}，"
                    f"与请求的项目 {project_id} 不符（证据不得跨项目漂流）"
                ),
                context={"qc_report_ref": qc_report_ref},
            )
        if facts.asset_version_id != asset_version_id:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail=(
                    f"QC 报告 {qc_report_ref} 绑定资产版本 "
                    f"{facts.asset_version_id}，与请求的 {asset_version_id} 不符"
                ),
                context={"qc_report_ref": qc_report_ref},
            )

        issues = [
            QCIssue.from_mapping(raw, index=index)
            for index, raw in enumerate(findings)
        ]
        new_id = session_id or f"qcs_{uuid.uuid4().hex}"
        session = QCReviewSession(
            entity_id=new_id,
            project_id=project_id,
            asset_version_id=asset_version_id,
            qc_report_ref=qc_report_ref,
            reviewers=list(reviewers),
            findings=issues,
            tenant_id=principal.tenant_id,
            organization_id=principal.organization_id,
            created_by=principal.subject_id,
            reference_set=dict(reference_set),
        )

        # 快速路径：同一报告已有会话 → 内容一致即幂等返回，否则拒绝。
        existing = await self._sessions.get_by_report(
            project_id=project_id, qc_report_ref=qc_report_ref
        )
        if existing is not None:
            return _assert_same_intake(existing, session)
        try:
            await self._sessions.add(session)
        except DuplicateEntityError as exc:
            # 并发开同一份报告：唯一约束裁决，输家回查后走同一判定。
            concurrent = await self._sessions.get_by_report(
                project_id=project_id, qc_report_ref=qc_report_ref
            )
            if concurrent is not None:
                return _assert_same_intake(concurrent, session)
            raise DomainError(
                ErrorRegistry.CONCURRENT_MODIFICATION,
                detail="复查会话并发创建冲突，请重试",
            ) from exc

        self._outbox.record(
            aggregate_type=ENTITY_TYPE,
            aggregate_id=session.entity_id,
            tenant_id=principal.tenant_id,
            event_type="QC_REVIEW_OPENED",
            payload={
                "session_id": session.entity_id,
                "project_id": session.project_id,
                "asset_version_id": session.asset_version_id,
                "qc_report_ref": session.qc_report_ref,
                "issue_count": len(session.findings),
                "status": session.status.value,
                "created_by": session.created_by,
                "tenant_id": str(principal.tenant_id),
            },
        )
        return session

    # ---------------------------------------------------------------- 裁决
    async def decide(
        self,
        *,
        session_id: str,
        project_id: str,
        target_status: QCReviewSessionStatus,
        decisions: Sequence[Mapping[str, Any]],
        principal: Principal,
    ) -> QCReviewSession:
        """追加人工结论并把会话推进到 ``target_status``。

        ``target_status`` 等于当前状态表示"只追加结论、暂不推进"——多位复核人
        分批给结论是常态，逼他们一次提交全部结论只会催生"先凑齐再提交"的
        假流程。真正的前置条件（结论齐备、问题闭合、边已登记）由领域层判定。

        ``project_id`` 用于**把授权对象与真实资源绑定**：授权层按请求体里的
        project_id 判定，若不与 ``session.project_id`` 比对，具备多项目权限的
        主体可以在 A 项目的授权下裁决 B 项目的会话（RLS 只能挡住"完全无权"，
        挡不住"有权但对象错了"）。
        """
        session = await self._sessions.get_by_id(session_id)
        if session is None:
            raise DomainError(
                ErrorRegistry.NOT_FOUND, detail=f"复查会话不存在: {session_id}"
            )
        if session.project_id != project_id:
            # 与"不存在"同码不同因：报告是对象级错误，不是权限拒绝。
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail=(
                    f"会话 {session_id} 归属项目 {session.project_id}，"
                    f"与请求项目 {project_id} 不一致（授权对象必须等于实际操作资源）"
                ),
                context={"session_id": session_id},
            )

        from_status = session.status
        expected_version = session.entity_version

        built = self._build_decisions(session, decisions, principal=principal)
        if (
            target_status is QCReviewSessionStatus.IN_REVIEW
            and session.status is QCReviewSessionStatus.OPEN
            and built
        ):
            # 只拦"首次进入复核时夹带结论"这一种：复核还没开始就有结论，
            # 等于把未开始的流程写成已完成。已在 IN_REVIEW 时追加结论是常态
            # （多位复核人分批提交），此时 target==当前状态、不推进，必须放行。
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail="进入 IN_REVIEW 之前不能有结论（复核尚未开始）",
                context={"session_id": session_id},
            )
        if built:
            session.record_decisions(built)
        if target_status is not session.status:
            session.advance(target_status, principal=principal)

        if built or session.status is not from_status:
            await self._sessions.update(session, expected_version=expected_version)
            # 本地版本号与库内对齐：conditional_update 只推进 +1，
            # 而 record_decisions / advance 各自加过一次，不回写会让响应里的
            # entity_version 比库内大，客户端后续 CAS 必然失败。
            session.entity_version = expected_version + 1
            self._outbox.record(
                aggregate_type=ENTITY_TYPE,
                aggregate_id=session.entity_id,
                tenant_id=principal.tenant_id,
                event_type="QC_REVIEW_DECIDED",
                payload={
                    "session_id": session.entity_id,
                    "project_id": session.project_id,
                    "asset_version_id": session.asset_version_id,
                    "from_status": from_status.value,
                    "to_status": session.status.value,
                    "decided_issue_ids": [d.issue_id for d in built],
                    "decided_by": principal.subject_id,
                    "tenant_id": str(principal.tenant_id),
                },
            )
        return session

    async def get_session(self, session_id: str) -> QCReviewSession:
        """读取会话（不可见即 NOT_FOUND，与列表口径一致）。"""
        session = await self._sessions.get_by_id(session_id)
        if session is None:
            raise DomainError(
                ErrorRegistry.NOT_FOUND, detail=f"复查会话不存在: {session_id}"
            )
        return session

    # ---------------------------------------------------------------- 内部
    def _build_decisions(
        self,
        session: QCReviewSession,
        raw_decisions: Sequence[Mapping[str, Any]],
        *,
        principal: Principal,
    ) -> list[ReviewDecision]:
        """把请求载荷转成领域裁决（身份一律取自 Principal）。"""
        index = session.issue_index
        built: list[ReviewDecision] = []
        for position, raw in enumerate(raw_decisions):
            if not isinstance(raw, Mapping):
                raise DomainError(
                    ErrorRegistry.VALIDATION_ERROR,
                    detail=f"decisions[{position}] 必须是对象",
                )
            issue_id = str(raw.get("issue_id") or "").strip()
            issue = index.get(issue_id)
            if issue is None:
                raise DomainError(
                    ErrorRegistry.VALIDATION_ERROR,
                    detail=(
                        f"decisions[{position}].issue_id={issue_id!r} 不在本次会话的"
                        f"缺陷列表内: {sorted(index)}"
                    ),
                    context={"issue_index": position, "issue_id": issue_id},
                )
            human_decision = _parse_decision(raw.get("human_decision"), position)
            built.append(
                ReviewDecision.record(
                    issue=issue,
                    human_decision=human_decision,
                    disposition=str(raw.get("disposition") or ""),
                    principal=principal,
                    waiver_authority=_optional_str(raw.get("waiver_authority")),
                    waiver_reason=_optional_str(raw.get("waiver_reason")),
                )
            )
        return built


def _parse_decision(value: object, position: int) -> QCIssueHumanDecision:
    try:
        return QCIssueHumanDecision(str(value))
    except ValueError as exc:
        raise DomainError(
            ErrorRegistry.VALIDATION_ERROR,
            detail=(
                f"decisions[{position}].human_decision={value!r} 不在契约取值内："
                f"{sorted(member.value for member in QCIssueHumanDecision)}"
            ),
            context={"issue_index": position},
        ) from exc


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _assert_same_intake(
    existing: QCReviewSession, incoming: QCReviewSession
) -> QCReviewSession:
    """幂等判定：同一报告的复查请求必须是同一件事。

    比对的是**输入**（资产版本、缺陷快照、参与人、参考集），不是状态与结论：
    会话跑了一段时间后再提交同样的开启请求，那是重放，应当拿回同一条会话，
    而不是"因为状态变了就报冲突"。
    """
    same = (
        existing.asset_version_id == incoming.asset_version_id
        and existing.qc_report_ref == incoming.qc_report_ref
        and [issue.to_dict() for issue in existing.findings]
        == [issue.to_dict() for issue in incoming.findings]
        and sorted(existing.reviewers) == sorted(incoming.reviewers)
        and existing.reference_set == incoming.reference_set
    )
    if same:
        return existing
    raise DomainError(
        ErrorRegistry.IDEMPOTENCY_CONFLICT,
        detail=(
            f"QC 报告 {incoming.qc_report_ref} 已有复查会话 "
            f"{existing.entity_id}，但本次提交的缺陷/参与人/参考集与既有会话不一致；"
            "同一份证据不能有两条互不相干的复核链"
        ),
        context={"session_id": existing.entity_id},
    )


__all__ = ["QCReviewService"]
