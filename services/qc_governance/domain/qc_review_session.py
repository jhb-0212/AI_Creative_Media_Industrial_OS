"""QCReviewSession 聚合（人机复核会话：缺陷、裁决与会话状态）。

契约对齐：
- ``contracts/schemas/object_schemas.schema.json`` → QCReviewSession：
  required = asset_version_id / decisions / findings / project_id /
  qc_report_ref / reference_set / reviewers / session_id + 信封。
  **契约没有独立的 session 主键字段**：``session_id`` 就是 ``entity_id``
  （与 AssetVersion 的 asset_version_id 同口径），不派生同值双列。
- ``contracts/state_machines/state_machines.yaml`` → QCReviewSession：
  OPEN→IN_REVIEW→DECISION_PENDING→RESOLVED→GATE_ELIGIBLE→CLOSED（C.7 权威；
  31.5 裁决 ④ 禁止 RESOLVED→CLOSED 跳关）。
- 动作：``QC.REVIEW.OPEN``（登记会话）与 ``QC.REVIEW.DECIDE``（记录结论并推进）。

四条结构性决定：

1. **结论只追加，不改写。** ``decisions`` 是历史；对同一缺陷重新裁决是追加
   一条新记录（"最新一条生效"），不是覆盖旧记录。改写历史会让"谁在什么时候
   判了什么"永久丢失，而 QC 的价值全在这条链上。
2. **推进状态要满足前置条件，而不只是边合法。** 只有登记边是不够的：
   ``IN_REVIEW→DECISION_PENDING`` 若允许在"还有缺陷没结论"时发生，
   会话就会带着未裁决的问题进入终局。前置条件见 ``_assert_ready_to``。
3. **红线不可豁免。** 4.3 规定 CRITICAL 必须阻断、"不能通过普通 Override
   绕过"；21.1 又要求豁免必须写明理由与授权。两者合起来：豁免必须带授权与
   理由，且只对非 CRITICAL 缺陷有效。
4. **``WAIVE_WITH_AUTHORITY`` 不是会话状态。** 它是 21.1 的人工结论类型；
   enum_registry 把它登记进状态集属差异，本实现不给它转移边（ADR-R36 §3）。
"""

from __future__ import annotations

from dataclasses import MISSING, dataclass, field
from dataclasses import fields as dataclass_fields
from datetime import datetime
from typing import Any
from uuid import UUID

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.identity.principal import Principal, SubjectType
from packages.shared.entity import SCHEMA_VERSION, utcnow
from services.qc_governance.domain.enums import (
    QCIssueHumanDecision,
    QCReviewSessionStatus,
)
from services.qc_governance.domain.qc_issue import QCIssue
from services.qc_governance.domain.state_machine import (
    allowed_targets,
    assert_transition_review_session,
)

#: 实体类型标识（契约 entity_type 取值）。
ENTITY_TYPE = "QCReviewSession"

#: 允许记录新结论的状态：复核中与待决阶段都能调整结论；RESOLVED 之后冻结
#: （否则"已解决的会话"可以事后被改写成另一个结论而不留痕迹）。
_DECISION_STATES: frozenset[QCReviewSessionStatus] = frozenset(
    {QCReviewSessionStatus.IN_REVIEW, QCReviewSessionStatus.DECISION_PENDING}
)


@dataclass(frozen=True)
class ReviewDecision:
    """一条针对具体缺陷的人工裁决（append-only；最新一条生效）。"""

    issue_id: str
    human_decision: QCIssueHumanDecision
    disposition: str
    decided_by: str
    decided_at: datetime
    waiver_authority: str | None = None
    waiver_reason: str | None = None

    @classmethod
    def record(
        cls,
        *,
        issue: QCIssue,
        human_decision: QCIssueHumanDecision,
        disposition: str,
        principal: Principal,
        waiver_authority: str | None = None,
        waiver_reason: str | None = None,
        decided_at: datetime | None = None,
    ) -> ReviewDecision:
        """由可信 Principal 构造一条裁决（唯一合法构造入口）。

        ``decided_by`` 不从请求体来：能填就是身份伪造，与 GateDecision 同口径。
        """
        if principal.subject_type is not SubjectType.HUMAN:
            # 21.2：AI 检测器负责发现、定位、量化与提供证据，不得单独裁决；
            # 人类负责意图解释、艺术判断、例外审批和最终责任。因此"谁下的结论"
            # 必须是人类主体——这条与「Agent 不得自批自用」同源，落在领域层
            # （授权层按动作角色拦一次，这里拦第二次：写进 decisions 的主体类型
            # 是要长期可追溯的事实）。
            raise DomainError(
                ErrorRegistry.AUTH_SCOPE_DENIED,
                detail=(
                    "QC 缺陷结论必须由人类主体作出（21.2：AI 只发现与提供证据，"
                    "不单独裁决）"
                ),
                context={"issue_id": issue.issue_id},
            )
        if human_decision is QCIssueHumanDecision.PENDING:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail=(
                    f"缺陷 {issue.issue_id} 的结论不能是 PENDING："
                    "「待定」不是结论，用它推进会话等于把未裁决的问题算成已裁决"
                ),
                context={"issue_id": issue.issue_id},
            )
        if not isinstance(disposition, str) or not disposition.strip():
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail=f"缺陷 {issue.issue_id} 的处置（disposition）不能为空",
                context={"issue_id": issue.issue_id},
            )

        if human_decision is QCIssueHumanDecision.WAIVE_WITH_AUTHORITY:
            if not (waiver_authority or "").strip() or not (waiver_reason or "").strip():
                raise DomainError(
                    ErrorRegistry.VALIDATION_ERROR,
                    detail=(
                        f"缺陷 {issue.issue_id} 选择豁免必须写明授权主体与理由"
                        "（21.1：豁免必须写明理由和授权）"
                    ),
                    context={"issue_id": issue.issue_id},
                )
            if issue.is_red_line:
                # 4.3：CRITICAL 必须阻断，不能通过普通 Override 绕过；
                # 21.1：不得豁免法律/版权/安全红线。两条在这里合成一道闸。
                raise DomainError(
                    ErrorRegistry.VALIDATION_ERROR,
                    detail=(
                        f"缺陷 {issue.issue_id} 为 CRITICAL（{issue.qc_dimension}），"
                        "红线级问题不得以任何理由豁免（4.3 / 21.1）"
                    ),
                    context={"issue_id": issue.issue_id, "severity": "CRITICAL"},
                )

        return cls(
            issue_id=issue.issue_id,
            human_decision=human_decision,
            disposition=disposition.strip(),
            decided_by=principal.subject_id,
            decided_at=decided_at or utcnow(),
            waiver_authority=(waiver_authority or None),
            waiver_reason=(waiver_reason or None),
        )

    def to_dict(self) -> dict[str, Any]:
        """序列化（只输出契约未禁止的裁决事实；无契约字段定义，故全量给出）。"""
        return {
            "issue_id": self.issue_id,
            "human_decision": self.human_decision.value,
            "disposition": self.disposition,
            "decided_by": self.decided_by,
            "decided_at": self.decided_at.isoformat(),
            "waiver_authority": self.waiver_authority,
            "waiver_reason": self.waiver_reason,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ReviewDecision:
        """从库内 JSONB 还原（只用于读路径；写路径必须走 ``record`` 以保留身份来源）。"""
        return cls(
            issue_id=str(raw["issue_id"]),
            human_decision=QCIssueHumanDecision(str(raw["human_decision"])),
            disposition=str(raw["disposition"]),
            decided_by=str(raw["decided_by"]),
            decided_at=datetime.fromisoformat(str(raw["decided_at"])),
            waiver_authority=raw.get("waiver_authority"),
            waiver_reason=raw.get("waiver_reason"),
        )


@dataclass
class QCReviewSession:
    """一次 QC 复查会话（围绕一个 QC 报告与一组缺陷）。"""

    entity_id: str
    project_id: str
    asset_version_id: str
    qc_report_ref: str
    reviewers: list[str]
    findings: list[QCIssue]
    tenant_id: UUID
    created_by: str
    reference_set: dict[str, Any] = field(default_factory=dict)
    decisions: list[ReviewDecision] = field(default_factory=list)
    status: QCReviewSessionStatus = QCReviewSessionStatus.OPEN
    organization_id: UUID | None = None
    schema_version: int = SCHEMA_VERSION
    entity_version: int = 1
    owner_module: str = "qc_governance"
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)
    audit_ref: str | None = None

    def __post_init__(self) -> None:
        """校验会话基本完整性（空会话、无缺陷会话不得存在）。"""
        if self.organization_id is None:
            self.organization_id = self.tenant_id
        for name in ("entity_id", "project_id", "asset_version_id", "qc_report_ref",
                     "created_by"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise DomainError(
                    ErrorRegistry.VALIDATION_ERROR, detail=f"{name} 不能为空"
                )
        if not self.reviewers:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail="reviewers 不能为空（复查会话至少要有参与人）",
            )
        if not self.findings:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail="findings 不能为空（没有缺陷就没有复查对象；空会话不得开启）",
            )
        if len({issue.issue_id for issue in self.findings}) != len(self.findings):
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail="findings 的 issue_id 重复（同一缺陷不能在一次会话里出现两次）",
            )
        if self.status is not QCReviewSessionStatus.OPEN:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail="会话必须从 OPEN 开始（其余状态只能经 QC.REVIEW.DECIDE 推进）",
            )
        if self.decisions:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail="新建会话不得带结论（结论只能经 QC.REVIEW.DECIDE 追加）",
            )

    # ------------------------------------------------------------------ 查询
    @classmethod
    def restore(cls, **fields: Any) -> QCReviewSession:
        """从持久化快照还原任意状态（**跳过**新建期校验）。

        为什么不复用构造器：``__post_init__`` 校验的是"新建会话该长什么样"
        （必须 OPEN、不得带结论），而读路径要还原 CLOSED 且带结论的既存会话。
        把两条语义混进一个构造器只有两种结果：读路径要传一堆开关，或者
        新建期的约束被悄悄放宽。因此显式分成两条入口，读路径不可能借此写入。
        """
        instance = object.__new__(cls)
        for f in dataclass_fields(cls):
            if f.name in fields:
                setattr(instance, f.name, fields[f.name])
            elif f.default is not MISSING:
                setattr(instance, f.name, f.default)
            elif f.default_factory is not MISSING:
                setattr(instance, f.name, f.default_factory())
            else:
                raise DomainError(
                    ErrorRegistry.INTERNAL_ERROR,
                    detail=f"还原会话缺少字段 {f.name}",
                )
        return instance

    @property
    def issue_index(self) -> dict[str, QCIssue]:
        """缺陷索引（按 issue_id）；重复 id 已在构造期拒绝。"""
        return {issue.issue_id: issue for issue in self.findings}

    def latest_decisions(self) -> dict[str, ReviewDecision]:
        """每个缺陷的最新结论（追加式历史的"生效值"口径）。"""
        latest: dict[str, ReviewDecision] = {}
        for decision in self.decisions:
            latest[decision.issue_id] = decision
        return latest

    def undecided_issue_ids(self) -> list[str]:
        """还没有任何结论的缺陷（按会话内顺序，便于稳定报错）。"""
        decided = self.latest_decisions()
        return [issue.issue_id for issue in self.findings if issue.issue_id not in decided]

    def unresolved_issue_ids(self) -> list[str]:
        """结论为 NEEDS_MORE_EVIDENCE 的缺陷（问题仍未闭合）。"""
        latest = self.latest_decisions()
        return [
            issue.issue_id
            for issue in self.findings
            if issue.issue_id in latest
            and latest[issue.issue_id].human_decision
            is QCIssueHumanDecision.NEEDS_MORE_EVIDENCE
        ]

    # ------------------------------------------------------------------ 变更
    def start_review(self, *, principal: Principal, at: datetime | None = None) -> None:
        """进入 IN_REVIEW（OPEN→IN_REVIEW），把发起人登记为参与人。"""
        self._assert_transition(QCReviewSessionStatus.IN_REVIEW)
        self.status = QCReviewSessionStatus.IN_REVIEW
        if principal.subject_id not in self.reviewers:
            self.reviewers = [*self.reviewers, principal.subject_id]
        self._touch(at)

    def record_decisions(
        self, decisions: list[ReviewDecision], *, at: datetime | None = None
    ) -> None:
        """追加人工结论（不推进状态；状态推进由 ``advance`` 显式完成）。"""
        if not decisions:
            return
        if self.status not in _DECISION_STATES:
            raise DomainError(
                ErrorRegistry.STATE_TRANSITION_DENIED,
                detail=(
                    f"当前状态 {self.status.value} 不接受新结论："
                    "结论只能在 IN_REVIEW / DECISION_PENDING 阶段记录，"
                    "RESOLVED 之后会话已冻结（重新结论须新开会话）"
                ),
                context={"status": self.status.value},
            )
        index = self.issue_index
        seen: set[str] = set()
        for decision in decisions:
            if decision.issue_id not in index:
                raise DomainError(
                    ErrorRegistry.VALIDATION_ERROR,
                    detail=(
                        f"结论指向的缺陷 {decision.issue_id} 不在本次会话内"
                        "（不能对会话外的缺陷下结论）"
                    ),
                    context={"issue_id": decision.issue_id},
                )
            if decision.issue_id in seen:
                raise DomainError(
                    ErrorRegistry.VALIDATION_ERROR,
                    detail=(
                        f"同一次请求对缺陷 {decision.issue_id} 给了多条结论"
                        "（一条缺陷一次只能有一个结论；改判请分两次提交）"
                    ),
                    context={"issue_id": decision.issue_id},
                )
            seen.add(decision.issue_id)
            if decision.decided_by not in self.reviewers:
                self.reviewers = [*self.reviewers, decision.decided_by]
        self.decisions = [*self.decisions, *decisions]
        self._touch(at)

    def advance(
        self,
        target: QCReviewSessionStatus,
        *,
        principal: Principal,
        at: datetime | None = None,
    ) -> None:
        """按登记边推进状态，并校验该步的前置条件。"""
        self._assert_transition(target)
        self._assert_ready_to(target)
        self.status = target
        if principal.subject_id not in self.reviewers:
            self.reviewers = [*self.reviewers, principal.subject_id]
        self._touch(at)
        if target is QCReviewSessionStatus.CLOSED and allowed_targets(target):
            # CLOSED 是终态：确认没有后续可走，避免"关掉了还能再推一次"。
            raise DomainError(
                ErrorRegistry.INTERNAL_ERROR,
                detail="CLOSED 不应有出边（契约状态链异常）",
            )

    # ------------------------------------------------------------------ 内部
    def _assert_transition(self, target: QCReviewSessionStatus) -> None:
        assert_transition_review_session(self.status, target)

    def _assert_ready_to(self, target: QCReviewSessionStatus) -> None:
        """前置条件：结论齐备 / 问题闭合。

        只校验"这一步该不该发生"，不校验"谁能做"——后者由授权层按
        ``QC.REVIEW.DECIDE`` 的角色与风险等级判定；领域层只保留一条不可让渡的
        事实约束：结论必须是人类作出（21.2），写在 ``ReviewDecision.record``。
        """
        if target is QCReviewSessionStatus.DECISION_PENDING:
            undecided = self.undecided_issue_ids()
            if undecided:
                raise DomainError(
                    ErrorRegistry.VALIDATION_ERROR,
                    detail=(
                        "还有缺陷没有人工结论，不能进入 DECISION_PENDING："
                        f"{undecided}"
                    ),
                    context={"undecided_issue_ids": undecided},
                )
        if target is QCReviewSessionStatus.RESOLVED:
            undecided = self.undecided_issue_ids()
            unresolved = self.unresolved_issue_ids()
            if undecided or unresolved:
                raise DomainError(
                    ErrorRegistry.VALIDATION_ERROR,
                    detail=(
                        "会话不能进入 RESOLVED："
                        f"未裁决 {undecided} / 证据不足待补 {unresolved}"
                    ),
                    context={
                        "undecided_issue_ids": undecided,
                        "unresolved_issue_ids": unresolved,
                    },
                )
        # 跳关拦截的真实来源是转移表（RESOLVED→CLOSED 未登记），这里再明确一次：
        # 只有通过 Gate 资格（GATE_ELIGIBLE）的会话才能关闭。
        if (
            target is QCReviewSessionStatus.CLOSED
            and self.status is not QCReviewSessionStatus.GATE_ELIGIBLE
        ):
            raise DomainError(
                ErrorRegistry.STATE_TRANSITION_DENIED,
                detail=(
                    f"会话 {self.entity_id} 当前为 {self.status.value}，"
                    "关闭前必须经 GATE_ELIGIBLE（31.5 裁决 ④ 禁止跳关）"
                ),
                context={
                    "from_status": self.status.value,
                    "to_status": target.value,
                    "allowed_targets": sorted(
                        t.value for t in allowed_targets(self.status)
                    ),
                },
            )

    def _touch(self, at: datetime | None) -> None:
        self.updated_at = at or utcnow()
        self.entity_version += 1

    # ---------------------------------------------------------------- 序列化
    def to_dict(self) -> dict[str, Any]:
        """序列化为契约结构（字段集 == QCReviewSession 契约声明）。"""
        return {
            "session_id": self.entity_id,
            "asset_version_id": self.asset_version_id,
            "project_id": self.project_id,
            "qc_report_ref": self.qc_report_ref,
            "reference_set": dict(self.reference_set),
            "reviewers": list(self.reviewers),
            "findings": [issue.to_dict() for issue in self.findings],
            "decisions": [decision.to_dict() for decision in self.decisions],
            "entity_id": self.entity_id,
            "entity_type": ENTITY_TYPE,
            "schema_version": self.schema_version,
            "entity_version": self.entity_version,
            "status": self.status.value,
            "owner_module": self.owner_module,
            "created_at": self.created_at.isoformat(),
            "created_by": self.created_by,
            "updated_at": self.updated_at.isoformat(),
            "audit_ref": self.audit_ref,
        }


__all__ = ["ENTITY_TYPE", "QCReviewSession", "ReviewDecision"]
