"""质量治理域 ORM 模型（独立 schema：qc_governance）。

数据所有权（14.2）：
- 本域拥有 `gate_decisions` 的写入权，且**只有本域角色** ``svc_qc_governance``
  能写。资产域与项目规划域都读不到它——需要 Gate 结论的地方通过本域服务取，
  而不是拼 SQL 直读（跨域直读会让"Gate 通过"失去单一裁决点）。
- 信封列复用 packages.shared.orm.EntityMixin（跨模块单一事实源）。

为什么 `decided_by_subject_type` 是内部列而不是契约字段：
- 契约 `GateDecision` 没有这个字段，接口不该多输出它；
- 但「Agent 不得自批自用」需要一条**数据层**的否决——授权层拦的是"发起请求
  的主体"，落库的是"被记为决策人的主体"，两者必须同时成立。把它做成列 +
  CHECK 约束，超级用户手工插入 Agent 裁决同样会被数据库拒绝。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from packages.shared.database import Base
from packages.shared.orm import EntityMixin

SCHEMA_NAME = "qc_governance"

#: 契约 7.4 声明的全部裁决取值。CHECK 用字面量列出（迁移里是历史快照，
#: 不能 import 代码——包 C4 已确立这条口径）。
DECISION_VALUES: tuple[str, ...] = ("APPROVED", "REJECTED", "RETURNED", "OVERRIDDEN")

#: QCReviewSession 的状态取值（C.7 状态链 + enum_registry 登记值，含
#: WAIVE_WITH_AUTHORITY——它是人工结论类型被登记为状态，保留取值、不建边）。
SESSION_STATUS_VALUES: tuple[str, ...] = (
    "OPEN",
    "IN_REVIEW",
    "DECISION_PENDING",
    "RESOLVED",
    "GATE_ELIGIBLE",
    "CLOSED",
    "WAIVE_WITH_AUTHORITY",
)


class GateDecisionModel(EntityMixin, Base):
    """`gate_decisions` 表模型（Gate 裁决事实，append-only）。

    主键就是 `entity_id`：契约 `GateDecision` **没有** `gate_decision_id` 字段，
    因此不派生同值列（同值双主键是「改一个忘一个」的漂移面）。

    幂等唯一索引覆盖 `(gate_instance_id, constraint_snapshot_id,
    ruleset_version, decision)`：对完全相同的输入与规则版本重复记同一条裁决
    没有信息增量，只让"被批过几次"变得可疑。并发下由约束兜底，不靠先查后插。
    领域对象允许同一 Gate 实例有多条不同裁决（重新评审 = 新快照 + 新裁决）。
    """

    __tablename__ = "gate_decisions"
    __table_args__ = (
        Index("ix_qc_governance_gate_decisions_project", "project_id"),
        Index(
            "ix_qc_governance_gate_decisions_instance",
            "gate_instance_id",
        ),
        # 幂等键（唯一）：同一实例 + 同一输入快照 + 同一规则版本 + 同一裁决只能一条。
        Index(
            "uq_qc_governance_gate_decisions_idempotency",
            "gate_instance_id",
            "constraint_snapshot_id",
            "ruleset_version",
            "decision",
            unique=True,
        ),
        CheckConstraint(
            f"decision IN ({', '.join(repr(v) for v in DECISION_VALUES)})",
            name="ck_qc_governance_gate_decisions_decision_registered",
        ),
        # 红线「Agent 不得自批自用」的数据层表达：不依赖任何应用代码记得校验。
        CheckConstraint(
            "decided_by_subject_type <> 'agent'",
            name="ck_qc_governance_gate_decisions_no_agent_decider",
        ),
        # 红线「缺证据不得 PASS」：APPROVED 必须有至少一条证据引用。
        # 拒绝/退回不强制（理由由 reason_code + comment 承载）。
        CheckConstraint(
            "decision <> 'APPROVED' OR jsonb_array_length(evidence_refs) > 0",
            name="ck_qc_governance_gate_decisions_approved_needs_evidence",
        ),
        {"schema": SCHEMA_NAME},
    )

    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    gate_key: Mapped[str] = mapped_column(String(32), nullable=False)
    gate_instance_id: Mapped[str] = mapped_column(String(64), nullable=False)
    decision: Mapped[str] = mapped_column(String(24), nullable=False)
    #: 裁决所依据的不可变约束快照（契约 7.4：「Gate 审批对象必须引用不可变版本」）。
    #: 它同时是判断"Gate 是否仍然有效"的锚点：快照变了，旧裁决仍在，但不再代表当前输入。
    constraint_snapshot_id: Mapped[str] = mapped_column(String(128), nullable=False)
    ruleset_version: Mapped[int] = mapped_column(Integer, nullable=False)
    entry_criteria: Mapped[str] = mapped_column(Text, nullable=False)
    required_artifacts: Mapped[str] = mapped_column(Text, nullable=False)
    reviewers: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    comment: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_refs: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    scope: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    decided_by: Mapped[str] = mapped_column(String(64), nullable=False)
    #: 内部列：决策主体类型（human / agent / service）。契约无此字段，不进响应。
    decided_by_subject_type: Mapped[str] = mapped_column(String(16), nullable=False)
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class QCReviewSessionModel(EntityMixin, Base):
    """`qc_review_sessions` 表模型（QC 复查会话：可推进，但内容列不可改写）。

    为什么它**可以** UPDATE 而 GateDecision 不行：会话是有生命周期的
    （OPEN→…→CLOSED），状态必须能推进；而裁决是不可变事实。二者的边界由
    守卫触发器写死：``status`` / ``decisions`` / ``reviewers`` 可变，
    ``findings`` / ``reference_set`` / 报告与资产版本引用等**内容列**一律不可变
    （复查的输入不能事后被换掉，否则"复核了什么"无法回答）。

    ``uq_..._report``：一份 QC 报告只对应一次复查。重复开启会让同一份证据
    出现两条互不相干的裁决链，Gate 侧无法判断该信哪一条。
    """

    __tablename__ = "qc_review_sessions"
    __table_args__ = (
        Index("ix_qc_governance_qc_review_sessions_project", "project_id"),
        Index(
            "ix_qc_governance_qc_review_sessions_asset_version",
            "asset_version_id",
        ),
        Index(
            "uq_qc_governance_qc_review_sessions_report",
            "tenant_id",
            "project_id",
            "qc_report_ref",
            unique=True,
        ),
        CheckConstraint(
            f"status IN ({', '.join(repr(v) for v in SESSION_STATUS_VALUES)})",
            name="ck_qc_governance_qc_review_sessions_status_registered",
        ),
        # 空会话不得存在：没有缺陷就没有复查对象（21.1）。
        CheckConstraint(
            "jsonb_array_length(findings) > 0",
            name="ck_qc_governance_qc_review_sessions_findings_not_empty",
        ),
        CheckConstraint(
            "jsonb_array_length(reviewers) > 0",
            name="ck_qc_governance_qc_review_sessions_reviewers_not_empty",
        ),
        {"schema": SCHEMA_NAME},
    )

    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    asset_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    qc_report_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    reference_set: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    reviewers: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    findings: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    decisions: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)


class QCReviewSessionStatusTransitionModel(Base):
    """`qc_review_session_status_transitions` 表模型（只读参考数据，ADR-R20）。

    qc_governance 自有的一行一类型——与质检裁决同域，因此不需要跨域桥接表：
    它只是"哪些边被登记过"的机读镜像，应用角色只有 SELECT，写入只发生在迁移里。
    """

    __tablename__ = "qc_review_session_status_transitions"
    __table_args__ = {"schema": SCHEMA_NAME}

    from_status: Mapped[str] = mapped_column(String(32), primary_key=True)
    to_status: Mapped[str] = mapped_column(String(32), primary_key=True)


__all__ = [
    "DECISION_VALUES",
    "SCHEMA_NAME",
    "SESSION_STATUS_VALUES",
    "GateDecisionModel",
    "QCReviewSessionModel",
    "QCReviewSessionStatusTransitionModel",
]
