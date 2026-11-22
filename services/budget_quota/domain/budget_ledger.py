"""BudgetLedger 聚合根（预算账本流水，追加式不可变，R16-D3）。

对齐契约：
- contracts/schemas/object_schemas.schema.json#BudgetLedger；
- contracts/enums/status_layers.yaml → envelope_only_sentinel = NO_LIFECYCLE。

这是**追加式不可变记录**，不是有生命周期的对象：BUDGET.SHOT.APPROVE 原子追加
一条流水，记录本身即代表「已入账」。余额由流水计算，不原地改写（与
BudgetLedgerTransaction 的 APPENDED 追加式不可变同口径）。transaction_kind
承载 Reserve / Commit / Release / Refund / Adjust 等流水类型，不是生命周期状态
（status_layers.yaml 已把 ADJUST / REFUND / REVERSAL 登记为 non_lifecycle_dimensions）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.shared.entity import Entity
from services.project_planning.domain.enums import NoLifecycleStatus

_OWNER_MODULE = "budget-quota"


def _blank(value: Any) -> bool:
    return value is None or not str(value).strip()


@dataclass
class BudgetLedger(Entity):
    """预算账本流水（追加即不可变，无生命周期）。"""

    ENTITY_TYPE = "BudgetLedger"

    owner_module: str = _OWNER_MODULE

    transaction_id: str = ""
    account_type: str = ""
    amount: int = 0
    currency: str = "CNY"
    transaction_kind: str = ""
    idempotency_key: str = ""
    approval_ref: str = ""
    source: str = ""
    status: NoLifecycleStatus = NoLifecycleStatus.NO_LIFECYCLE

    def __post_init__(self) -> None:
        super()._validate_envelope()
        self.transaction_id = self.transaction_id.strip() or self.entity_id
        for name in (
            "transaction_id",
            "account_type",
            "transaction_kind",
            "idempotency_key",
            "approval_ref",
            "source",
        ):
            if _blank(getattr(self, name)):
                raise DomainError(
                    ErrorRegistry.VALIDATION_ERROR, detail=f"{name} 不能为空"
                )
        for name, max_len in (
            ("transaction_id", 64),
            ("account_type", 32),
            ("currency", 8),
            ("transaction_kind", 32),
            ("idempotency_key", 128),
            ("approval_ref", 128),
            ("source", 128),
        ):
            if len(getattr(self, name)) > max_len:
                raise DomainError(
                    ErrorRegistry.VALIDATION_ERROR,
                    detail=f"{name} 长度超过上限 {max_len}",
                )
        if type(self.amount) is not int:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail=f"amount 必须是整数，实际为 {self.amount!r}",
            )

    def to_dict(self) -> dict[str, Any]:
        data = self._envelope_dict()
        data.update(
            {
                "transaction_id": self.transaction_id,
                "account_type": self.account_type,
                "amount": self.amount,
                "currency": self.currency,
                "transaction_kind": self.transaction_kind,
                "idempotency_key": self.idempotency_key,
                "approval_ref": self.approval_ref,
                "source": self.source,
            }
        )
        return data


__all__ = ["BudgetLedger"]
