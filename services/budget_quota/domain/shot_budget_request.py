"""ShotBudgetRequest 聚合根（单镜头预算申请，R16-D3）。

对齐契约：
- contracts/schemas/object_schemas.schema.json#ShotBudgetRequest；
- contracts/state_machines/state_machines.yaml → objects → ShotBudgetRequest
  （DRAFT → APPROVED / REJECTED，两条边归 BUDGET.SHOT.APPROVE）。

两步审批与职责分离：BUDGET.SHOT.REQUEST 创建 DRAFT；BUDGET.SHOT.APPROVE 推进
APPROVED / REJECTED。APPROVED 时要求 approver 非空且 != requested_by（自批拒绝，
与剧本锁定两步审批同一条纪律），并在同一事务内原子追加 BudgetLedger 流水。
Quality Reserve 不得被挪用（ACT-BUDGET-NEG-002）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.shared.entity import Entity
from services.project_planning.domain.enums import ShotBudgetRequestStatus
from services.project_planning.domain.state_machine import SHOT_BUDGET_REQUEST_LIFECYCLE

_OWNER_MODULE = "budget-quota"


def _blank(value: Any) -> bool:
    return value is None or not str(value).strip()


@dataclass
class ShotBudgetRequest(Entity):
    """单镜头预算申请（DRAFT 出生，APPROVED/REJECTED 终态）。"""

    ENTITY_TYPE = "ShotBudgetRequest"

    owner_module: str = _OWNER_MODULE

    request_id: str = ""
    project_id: str = ""
    shot_id: str = ""
    account_type: str = ""
    purpose: str = ""
    cost_category: str = ""
    amount_limit: int = 0
    requested_by: str = ""
    approver: str = ""
    decision_reason: str = ""
    status: ShotBudgetRequestStatus = ShotBudgetRequestStatus.DRAFT

    def __post_init__(self) -> None:
        super()._validate_envelope()
        self.request_id = self.request_id.strip() or self.entity_id
        for name in (
            "request_id",
            "project_id",
            "shot_id",
            "account_type",
            "purpose",
            "cost_category",
            "requested_by",
        ):
            if _blank(getattr(self, name)):
                raise DomainError(ErrorRegistry.VALIDATION_ERROR, detail=f"{name} 不能为空")
        for name, max_len in (
            ("request_id", 64),
            ("project_id", 64),
            ("shot_id", 64),
            ("account_type", 32),
            ("purpose", 128),
            ("cost_category", 64),
            ("requested_by", 64),
            ("approver", 64),
            ("decision_reason", 200),
        ):
            if len(getattr(self, name)) > max_len:
                raise DomainError(
                    ErrorRegistry.VALIDATION_ERROR,
                    detail=f"{name} 长度超过上限 {max_len}",
                )
        # amount_limit 类型判定与 ShotSpec.duration_ms 同因：>= 0 放行浮点与 bool。
        if type(self.amount_limit) is not int:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail=f"amount_limit 必须是整数，实际为 {self.amount_limit!r}",
            )
        if self.amount_limit < 0:
            raise DomainError(ErrorRegistry.VALIDATION_ERROR, detail="amount_limit 不能为负")

    def transition_to(
        self,
        target: ShotBudgetRequestStatus,
        audit_ref: str,
        action: str,
    ) -> ShotBudgetRequest:
        """执行 DRAFT → APPROVED / REJECTED。

        APPROVED 要求审批人非空且与申请人职责分离；拒绝理由只在 REJECTED 时强制。
        """
        SHOT_BUDGET_REQUEST_LIFECYCLE.edge_owned_by(self.status, target, action)
        if target is ShotBudgetRequestStatus.APPROVED:
            if _blank(self.approver):
                raise DomainError(
                    ErrorRegistry.VALIDATION_ERROR,
                    detail="批准必须给出 approver（审批人不能为空）",
                )
            if self.approver.strip() == self.requested_by.strip():
                raise DomainError(
                    ErrorRegistry.VALIDATION_ERROR,
                    detail="申请人与审批人不得为同一主体（职责分离）",
                )
        if target is ShotBudgetRequestStatus.REJECTED and _blank(self.decision_reason):
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail="拒绝必须给出 decision_reason（拒绝理由不能为空）",
            )
        self.status = target
        self._advance_version(audit_ref)
        return self

    def to_dict(self) -> dict[str, Any]:
        data = self._envelope_dict()
        data.update(
            {
                "request_id": self.request_id,
                "project_id": self.project_id,
                "shot_id": self.shot_id,
                "account_type": self.account_type,
                "purpose": self.purpose,
                "cost_category": self.cost_category,
                "amount_limit": self.amount_limit,
                "requested_by": self.requested_by,
                "approver": self.approver,
                "decision_reason": self.decision_reason,
            }
        )
        return data


__all__ = ["ShotBudgetRequest"]
