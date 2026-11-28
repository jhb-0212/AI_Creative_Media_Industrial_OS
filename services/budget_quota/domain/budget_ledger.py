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
from services.budget_quota.domain.enums import SETTLEMENT_KINDS, BudgetTransactionKind
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
    # RELEASE / REFUND 指回被释放/冲销的那条 RESERVE 流水；RESERVE 自身为空。
    reservation_ref: str = ""
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
                raise DomainError(ErrorRegistry.VALIDATION_ERROR, detail=f"{name} 不能为空")
        # 受控维度：不在枚举里就当场拒绝。库层还有 CHECK，两者同一集合派生。
        try:
            kind = BudgetTransactionKind(self.transaction_kind)
        except ValueError as exc:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail=f"transaction_kind 不在受控集合: {self.transaction_kind!r}",
            ) from exc
        # 释放/冲销必须指回预留言本；否则「释放的是哪一笔」无从判断，
        # 余额口径会退化成「按账户类型猜」。
        if kind in SETTLEMENT_KINDS and _blank(self.reservation_ref):
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail=f"{kind.value} 必须携带 reservation_ref（指回被处理的 RESERVE 流水）",
            )
        if self.amount < 0:
            raise DomainError(ErrorRegistry.VALIDATION_ERROR, detail="amount 不能为负")
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
                "reservation_ref": self.reservation_ref,
            }
        )
        return data


__all__ = ["BudgetLedger"]
