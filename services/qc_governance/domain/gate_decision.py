"""GateDecision 对象（Gate 裁决事实：append-only，不声明未发生的通过）。

契约对齐：
- `contracts/schemas/object_schemas.schema.json` → GateDecision：
  必填 = `comment` / `constraint_snapshot_id` / `decided_at` / `decided_by` /
  `decision` / `entry_criteria` / `evidence_refs` / `gate_instance_id` /
  `gate_key` / `project_id` / `reason_code` / `required_artifacts` /
  `reviewers` / `ruleset_version` / `scope` + 实体信封。
  **注意契约没有 `gate_decision_id`**：主键就是 `entity_id`，不额外派生一个
  同值字段（同值双列是"改一个忘一个"的漂移面）。
- `contracts/action_registry.yaml` → `PRODUCTION.GATE.APPROVE`（CRITICAL，
  「对应 Gate 授权人类职责主体」，`approval_requirement` = 「强制角色分离、
  证据与审批记录」，负向用例 `ACT-GATE-NEG-001`「Agent/非授权人审批拒绝」）。
- 正文 7.4 Gate 通用契约；7.5 Gate 0—4 主表；7.3 A2 子 Gate。

五条结构性决定：

1. **这是一条"事实"，不是一张"可改的表"。** 端口没有 save，数据库只授
   `SELECT, INSERT` 并加 append-only 触发器。裁决可以重做（新建一条），
   但不能把"当时批了什么"改写。审计链的价值全在这一点上。
2. **`decided_by` 只能来自可信 Principal。** 领域对象接受 `decided_by` 与
   `decided_by_subject_type` 两个参数，但服务层只能从 ``Principal`` 填，
   路由请求体**不接受**这两个字段。`decided_by_subject_type` 是内部列
   （契约没有），用途是把「Agent 不得自批」压到数据库 CHECK 上——见
   `data/migrations/versions/e8f9a0b1c2d3_*.py`。
3. **`status` 恒为 RECORDED。** 决策事实没有生命周期；"Gate 是否仍然有效"
   是派生问题（见 ``enums.GateDecisionStatus`` 的说明），不是这一行的状态。
   让它可变更等于允许改写历史。
4. **APPROVED 必须有证据；四种裁决都必须有理由。** 前者是"缺证据不得 PASS"
   在这条链上的落地（领域校验 + DB CHECK 双份），后者由 `reason_code`
   必填保证——无理由的裁决无法被复核。
5. **`OVERRIDDEN` 可表示但不可进入。** 契约 7.4 列了它，但没有任何字段
   说明"覆盖了哪一条决策"。接受它会产出无法追溯的裁决，因此领域层拒绝，
   并登记为债务（ADR-R23）。枚举值仍保留，否则 ``enum_parity`` 会报缺值。

关于 `reviewers`：必须包含 `decided_by`。这不是新增业务规则，而是一致性
约束——决策人就是参与审批的人；若 `reviewers` 里没有他，这条记录描述的
是"某人以别人的名义作出裁决"，正是 `ACT-GATE-NEG-001` 要拦的形态。
多人会签（7.4「审批顺序、是否需要多人会签」）未实现：`reviewers` 只作为
参与人记录，**不构成会签证据**，ADR-R23 已登记。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.identity.principal import Principal, SubjectType
from packages.shared.entity import SCHEMA_VERSION, utcnow
from services.qc_governance.domain.enums import (
    GateDecisionStatus,
    GateDecisionValue,
    GateKey,
)

#: 允许进入的裁决取值。`OVERRIDDEN` 刻意不在内（缺"覆盖了谁"的引用字段）。
_ENTERABLE_DECISIONS: frozenset[GateDecisionValue] = frozenset(
    {
        GateDecisionValue.APPROVED,
        GateDecisionValue.REJECTED,
        GateDecisionValue.RETURNED,
    }
)

#: 实体类型标识（契约 `entity_type` 取值）。
ENTITY_TYPE = "GateDecision"


@dataclass
class GateDecision:
    """一条 Gate 裁决记录（创建即不可变）。

    字段命名与契约一一对应；``decided_by_subject_type`` / ``tenant_id`` /
    ``organization_id`` 是内部列，不进 ``to_dict()``。
    """

    entity_id: str
    project_id: str
    gate_key: GateKey
    gate_instance_id: str
    decision: GateDecisionValue
    constraint_snapshot_id: str
    ruleset_version: int
    entry_criteria: str
    required_artifacts: str
    reviewers: list[str]
    reason_code: str
    comment: str
    evidence_refs: list[str]
    scope: dict[str, Any]
    decided_by: str
    created_by: str
    tenant_id: UUID
    organization_id: UUID | None = None
    #: 决策主体的类型（内部列）。数据库 CHECK 拒绝 'agent'：红线「Agent 不得
    #: 自批自用」在数据层的最后一道，不依赖任何应用代码记得校验。
    decided_by_subject_type: SubjectType = SubjectType.HUMAN
    decided_at: datetime = field(default_factory=utcnow)
    status: GateDecisionStatus = GateDecisionStatus.RECORDED
    schema_version: int = SCHEMA_VERSION
    entity_version: int = 1
    owner_module: str = "qc_governance"
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)
    audit_ref: str | None = None

    def __post_init__(self) -> None:
        """校验裁决完整性（缺证据/缺理由/自批的裁决不得存在）。"""
        if self.organization_id is None:
            self.organization_id = self.tenant_id

        for name in (
            "entity_id",
            "project_id",
            "gate_instance_id",
            "constraint_snapshot_id",
            "entry_criteria",
            "required_artifacts",
            "reason_code",
            "comment",
            "decided_by",
            "created_by",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise DomainError(
                    ErrorRegistry.VALIDATION_ERROR, detail=f"{name} 不能为空"
                )

        if self.ruleset_version < 1:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail="ruleset_version 必须 >= 1（规则版本是裁决的判定依据）",
            )
        if not self.scope:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail="scope 不能为空（裁决必须说明作用于项目/集/场景/镜头）",
            )
        if not self.reviewers:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail="reviewers 不能为空（一条裁决至少有一个评审人）",
            )
        if self.decided_by not in self.reviewers:
            # 一致性而非新增规则：决策人不在评审人里，等于"以他人名义审批"。
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail="decided_by 必须出现在 reviewers 中（决策人即参与审批的人）",
            )
        if self.decided_by_subject_type is SubjectType.AGENT:
            # 红线：Agent 不得自批自用。授权层已按 risk_level=CRITICAL 拦一次，
            # 这里拦第二次是因为"决策主体类型"是要落库的事实——授权层拦的是
            # 发起请求的主体，落库的是被记为决策人的主体，两者必须同时成立。
            raise DomainError(
                ErrorRegistry.AUTH_SCOPE_DENIED,
                detail="Agent 不得作为 Gate 决策人（红线：Agent 不得自批自用）",
            )
        if self.decision not in _ENTERABLE_DECISIONS:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail=(
                    f"裁决取值 {self.decision.value} 当前不可进入："
                    "契约未定义它覆盖的是哪一条决策，接受会产生无法追溯的裁决"
                ),
            )
        if (
            self.decision is GateDecisionValue.APPROVED
            and not self.evidence_refs
        ):
            # 红线在 Gate 链上的表达：没有证据的"通过"就是一次无法复核的放行。
            # 拒绝/退回不强制证据（理由由 reason_code + comment 承载）。
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail="APPROVED 裁决必须提供 evidence_refs（缺证据不得 PASS）",
            )
        if self.status is not GateDecisionStatus.RECORDED:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail="GateDecision 是事实记录，status 只能是 RECORDED",
            )

    @classmethod
    def record(
        cls,
        *,
        entity_id: str,
        project_id: str,
        gate_key: GateKey,
        gate_instance_id: str,
        decision: GateDecisionValue,
        constraint_snapshot_id: str,
        ruleset_version: int,
        entry_criteria: str,
        required_artifacts: str,
        reviewers: list[str],
        reason_code: str,
        comment: str,
        evidence_refs: list[str],
        scope: dict[str, Any],
        principal: Principal,
    ) -> GateDecision:
        """由可信 Principal 构造一条裁决（唯一合法构造入口）。

        为什么提供工厂而不是让服务层逐字段填 ``decided_by``：只要有一个调用点
        从请求体拼 ``decided_by``，身份来源就不再可信。把"决策人从 Principal 取"
        收进构造函数，调用方**没有**传错的机会——这是结构约束，不是约定。

        ``decided_at`` 取服务端时间：客户端提供的时间可以伪造"我在变更前就批了"。
        补录历史裁决（决策实际发生在过去）当前不支持，ADR-R23 已登记。
        """
        return cls(
            entity_id=entity_id,
            project_id=project_id,
            gate_key=gate_key,
            gate_instance_id=gate_instance_id,
            decision=decision,
            constraint_snapshot_id=constraint_snapshot_id,
            ruleset_version=ruleset_version,
            entry_criteria=entry_criteria,
            required_artifacts=required_artifacts,
            reviewers=list(reviewers),
            reason_code=reason_code,
            comment=comment,
            evidence_refs=list(evidence_refs),
            scope=dict(scope),
            decided_by=principal.subject_id,
            decided_by_subject_type=principal.subject_type,
            created_by=principal.subject_id,
            tenant_id=principal.tenant_id,
            organization_id=principal.organization_id,
            decided_at=utcnow(),
        )

    @property
    def is_approval(self) -> bool:
        """是否为一个"通过"裁决（供 Gate4 PASS 解析使用）。"""
        return self.decision is GateDecisionValue.APPROVED

    def idempotency_key(self) -> tuple[str, str, int, str]:
        """幂等键：同一 Gate 实例 + 同一输入快照 + 同一规则版本 + 同一裁决。

        四者全同意味着"对完全相同的输入再做一次相同判定"，没有信息增量，
        只会让"这个 Gate 被批过几次"变得可疑。数据库上有对应唯一索引，
        并发下由约束兜底而不是靠先查后插。
        """
        return (
            self.gate_instance_id,
            self.constraint_snapshot_id,
            self.ruleset_version,
            self.decision.value,
        )

    def same_content(self, other: GateDecision) -> bool:
        """判断两条记录是否描述同一件事（幂等重放时区分"重放"与"冲突写"）。

        只比对**契约声明的事实字段**。``entity_id`` 不在内：客户端可以用不同
        id 重放同一裁决，那仍然是同一件事（幂等键才是判定依据）。
        """
        return self.idempotency_key() == other.idempotency_key() and (
            self.decided_by == other.decided_by
            and self.reason_code == other.reason_code
            and self.comment == other.comment
            and sorted(self.evidence_refs) == sorted(other.evidence_refs)
            and sorted(self.reviewers) == sorted(other.reviewers)
        )

    def to_dict(self) -> dict[str, Any]:
        """序列化为契约结构（字段集 == GateDecision 契约声明）。

        刻意不输出：``tenant_id`` / ``organization_id``（内部隔离键）、
        ``decided_by_subject_type``（内部列）、``gate_decision_id``（契约无此字段）。
        """
        return {
            "comment": self.comment,
            "constraint_snapshot_id": self.constraint_snapshot_id,
            "decided_at": self.decided_at.isoformat(),
            "decided_by": self.decided_by,
            "decision": self.decision.value,
            "entry_criteria": self.entry_criteria,
            "evidence_refs": list(self.evidence_refs),
            "gate_instance_id": self.gate_instance_id,
            "gate_key": self.gate_key.value,
            "project_id": self.project_id,
            "reason_code": self.reason_code,
            "required_artifacts": self.required_artifacts,
            "reviewers": list(self.reviewers),
            "ruleset_version": self.ruleset_version,
            "scope": dict(self.scope),
            # 信封（契约 required 含这些字段）
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


__all__ = ["ENTITY_TYPE", "GateDecision"]
