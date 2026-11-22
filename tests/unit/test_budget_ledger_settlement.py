"""账本释放 / 冲销（Phase 2 §2）的单元测试。

四条失效模式各一例：
- 对非 RESERVE 流水结算（会让余额无限下探）；
- 超过预留金额（凭空放出不存在的钱）；
- 重复结算（同一笔预留被释放两次）；
- 跨租户结算（拿别的组织的预留做自己的余额）。
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.shared.in_memory import InMemoryOutboxWriter
from services.budget_quota.domain.budget_ledger import BudgetLedger
from services.budget_quota.service.budget_service import BudgetService

_TENANT = uuid4()


class _FakeLedgerRepo:
    """最小账本仓储：内存字典 + find_settlement（与服务层契约一致）。"""

    def __init__(self) -> None:
        self.rows: dict[str, BudgetLedger] = {}

    async def add(self, ledger: BudgetLedger) -> BudgetLedger:
        if ledger.idempotency_key in {row.idempotency_key for row in self.rows.values()}:
            raise DomainError(ErrorRegistry.IDEMPOTENCY_CONFLICT, detail="幂等键冲突")
        self.rows[ledger.entity_id] = ledger
        return ledger

    async def get_by_id(self, entity_id: str) -> BudgetLedger | None:
        return self.rows.get(entity_id)

    async def find_settlement(
        self, *, reservation_ref: str, transaction_kind: str
    ) -> BudgetLedger | None:
        for row in self.rows.values():
            if (
                row.reservation_ref == reservation_ref
                and row.transaction_kind == transaction_kind
            ):
                return row
        return None


def _reserve(*, amount: int = 1000, tenant_id: Any = _TENANT) -> BudgetLedger:
    return BudgetLedger(
        entity_id=str(uuid4()),
        created_by="producer",
        tenant_id=tenant_id,
        transaction_id="",
        account_type="SHOT_BUDGET",
        amount=amount,
        currency="CNY",
        transaction_kind="RESERVE",
        idempotency_key=f"shot-budget:{uuid4()}",
        approval_ref="approval-1",
        source="request-1",
    )


def _service() -> tuple[BudgetService, _FakeLedgerRepo, InMemoryOutboxWriter]:
    repo = _FakeLedgerRepo()
    outbox = InMemoryOutboxWriter()
    return BudgetService(repo, repo, outbox), repo, outbox


async def test_reserve_has_empty_reservation_ref() -> None:
    service, repo, _outbox = _service()
    reserve = _reserve()
    await repo.add(reserve)
    assert reserve.reservation_ref == ""
    assert service is not None


async def test_release_append_only_and_points_back() -> None:
    service, repo, outbox = _service()
    reserve = _reserve(amount=1000)
    await repo.add(reserve)

    released = await service.release_budget(
        reservation_ledger_id=reserve.entity_id,
        amount=400,
        actor="producer",
        tenant_id=_TENANT,
        reason="镜头改用更便宜的模型",
    )

    assert released.transaction_kind == "RELEASE"
    assert released.reservation_ref == reserve.entity_id
    assert released.amount == 400
    # 原流水不可变：预留金额与幂等键都没被改写。
    assert repo.rows[reserve.entity_id].amount == 1000
    assert outbox.events[-1]["event_type"] == "SHOT_BUDGET_RELEASED"
    assert outbox.events[-1]["payload"]["reservation_ref"] == reserve.entity_id
    assert outbox.events[-1]["payload"]["amount"] == 400


async def test_double_release_is_rejected() -> None:
    service, repo, _outbox = _service()
    reserve = _reserve(amount=1000)
    await repo.add(reserve)
    await service.release_budget(
        reservation_ledger_id=reserve.entity_id,
        amount=100,
        actor="producer",
        tenant_id=_TENANT,
    )

    with pytest.raises(DomainError):
        await service.release_budget(
            reservation_ledger_id=reserve.entity_id,
            amount=100,
            actor="producer",
            tenant_id=_TENANT,
        )


async def test_release_over_reserved_amount_is_rejected() -> None:
    service, repo, _outbox = _service()
    reserve = _reserve(amount=1000)
    await repo.add(reserve)

    with pytest.raises(DomainError):
        await service.release_budget(
            reservation_ledger_id=reserve.entity_id,
            amount=1001,
            actor="producer",
            tenant_id=_TENANT,
        )


async def test_settle_only_reserve_rows() -> None:
    service, repo, _outbox = _service()
    reserve = _reserve(amount=1000)
    await repo.add(reserve)
    released = await service.release_budget(
        reservation_ledger_id=reserve.entity_id,
        amount=100,
        actor="producer",
        tenant_id=_TENANT,
    )

    # 对一条 RELEASE 再做 RELEASE：余额会被无限下探，必须拒绝。
    with pytest.raises(DomainError):
        await service.release_budget(
            reservation_ledger_id=released.entity_id,
            amount=50,
            actor="producer",
            tenant_id=_TENANT,
        )


async def test_cross_tenant_settlement_is_rejected() -> None:
    service, repo, _outbox = _service()
    reserve = _reserve(amount=1000)
    await repo.add(reserve)

    with pytest.raises(DomainError):
        await service.refund_budget(
            reservation_ledger_id=reserve.entity_id,
            amount=100,
            actor="producer",
            tenant_id=uuid4(),
        )


async def test_refund_is_a_second_kind_on_same_reservation() -> None:
    service, repo, outbox = _service()
    reserve = _reserve(amount=1000)
    await repo.add(reserve)

    refund = await service.refund_budget(
        reservation_ledger_id=reserve.entity_id,
        amount=200,
        actor="producer",
        tenant_id=_TENANT,
        reason="结算后金额下调",
    )

    assert refund.transaction_kind == "REFUND"
    assert refund.reservation_ref == reserve.entity_id
    assert outbox.events[-1]["event_type"] == "SHOT_BUDGET_REFUNDED"
    # 释放与冲销是两种不同事实，可以同时存在（唯一索引按 kind 区分）。
    assert refund.idempotency_key != f"budget-release:::{reserve.entity_id}"


async def test_domain_rejects_uncontrolled_kind() -> None:
    with pytest.raises(DomainError):
        BudgetLedger(
            entity_id=str(uuid4()),
            created_by="producer",
            tenant_id=_TENANT,
            transaction_id="",
            account_type="SHOT_BUDGET",
            amount=10,
            currency="CNY",
            transaction_kind="RELEASED",
            idempotency_key="k-1",
            approval_ref="approval-1",
            source="request-1",
        )


async def test_settlement_requires_reservation_ref() -> None:
    with pytest.raises(DomainError):
        BudgetLedger(
            entity_id=str(uuid4()),
            created_by="producer",
            tenant_id=_TENANT,
            transaction_id="",
            account_type="SHOT_BUDGET",
            amount=10,
            currency="CNY",
            transaction_kind="RELEASE",
            idempotency_key="k-2",
            approval_ref="approval-1",
            source="request-1",
        )
