"""GateDecision 聚合根（Gate 决策记录，envelope_only / immutable_record）。

对齐契约：
- contracts/schemas/object_schemas.schema.json#GateDecision（envelope_only，
  status 唯一允许 NO_LIFECYCLE 哨兵）；
- 31.6 Action Registry（PRODUCTION.GATE.APPROVE 的 resource_type）。

这是**追加式不可变记录**：一次 Gate 审批产出一条决策（APPROVED / REJECTED），
记录本身即代表「这个 Gate 已经裁决」。Gate 的生命周期（如 CreatorGate 的
PENDING→PASS）归 Gate 定义对象，不属于决策记录——两者不是同一个状态机。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.shared.entity import Entity
from services.project_planning.domain.enums import NoLifecycleStatus

_OWNER_MODULE = "qc-governance"


def _blank(value: Any) -> bool:
    return value is None or not str(value).strip()


@dataclass
class GateDecision(Entity):
    """Gate 决策记录（追加即不可变，无生命周期）。"""

    ENTITY_TYPE = "GateDecision"

    owner_module: str = _OWNER_MODULE

    comment: str = ""
    constraint_snapshot_id: str = ""
    decided_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    decided_by: str = ""
    decision: str = ""
    entry_criteria: str = ""
    evidence_refs: list[str] = field(default_factory=list)
    gate_instance_id: str = ""
    gate_key: str = ""
    project_id: str = ""
    reason_code: str = ""
    required_artifacts: str = ""
    reviewers: list[str] = field(default_factory=list)
    ruleset_version: int = 1
    scope: dict[str, Any] = field(default_factory=dict)
    status: NoLifecycleStatus = NoLifecycleStatus.NO_LIFECYCLE

    def __post_init__(self) -> None:
        super()._validate_envelope()
        # 只强制决策的四个核心要素非空；constraint_snapshot_id / gate_instance_id /
        # reason_code / entry_criteria / required_artifacts 在 schema 中是「必含键」，
        # 但允许空值（不同 Gate 的信息完备度不同，禁止空值会拒掉合法决策）。
        for name in ("decision", "decided_by", "gate_key", "project_id"):
            if _blank(getattr(self, name)):
                raise DomainError(ErrorRegistry.VALIDATION_ERROR, detail=f"{name} 不能为空")
        if self.decision not in ("APPROVED", "REJECTED"):
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail=f"decision 必须是 APPROVED 或 REJECTED，实际为 {self.decision!r}",
            )
        for name, max_len in (
            ("constraint_snapshot_id", 128),
            ("decided_by", 64),
            ("decision", 16),
            ("gate_instance_id", 64),
            ("gate_key", 64),
            ("project_id", 64),
            ("reason_code", 64),
            ("comment", 500),
            ("entry_criteria", 500),
            ("required_artifacts", 500),
        ):
            if len(getattr(self, name)) > max_len:
                raise DomainError(
                    ErrorRegistry.VALIDATION_ERROR,
                    detail=f"{name} 长度超过上限 {max_len}",
                )

    def to_dict(self) -> dict[str, Any]:
        data = self._envelope_dict()
        data.update(
            {
                "comment": self.comment,
                "constraint_snapshot_id": self.constraint_snapshot_id,
                "decided_at": self.decided_at.isoformat(),
                "decided_by": self.decided_by,
                "decision": self.decision,
                "entry_criteria": self.entry_criteria,
                "evidence_refs": list(self.evidence_refs),
                "gate_instance_id": self.gate_instance_id,
                "gate_key": self.gate_key,
                "project_id": self.project_id,
                "reason_code": self.reason_code,
                "required_artifacts": self.required_artifacts,
                "reviewers": list(self.reviewers),
                "ruleset_version": self.ruleset_version,
                "scope": dict(self.scope),
            }
        )
        return data


__all__ = ["GateDecision"]
