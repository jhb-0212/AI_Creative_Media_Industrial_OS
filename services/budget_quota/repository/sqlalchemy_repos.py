"""单镜预算申请与预算账本的 SQLAlchemy 仓储（R16，project_planning schema 过渡期）。"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.shared.port_errors import translate_write_error
from packages.shared.versioning import _orm_update_values, conditional_update
from services.budget_quota.domain.budget_ledger import BudgetLedger
from services.budget_quota.domain.enums import SETTLEMENT_KINDS
from services.budget_quota.domain.shot_budget_request import ShotBudgetRequest
from services.budget_quota.repository.orm import (
    BudgetLedgerModel,
    ShotBudgetRequestModel,
)
from services.project_planning.domain.enums import (
    NoLifecycleStatus,
    ShotBudgetRequestStatus,
)

# 结算类流水（RELEASE / REFUND）的取值集合：从领域枚举派生，不在此手写字符串。
# 写死一份的下场是「加了新结算种类，累计口径静默漏算」——漏算不会报错，
# 它只是让「累计结算」永远小于真实值。
_SETTLEMENT_KIND_VALUES: tuple[str, ...] = tuple(
    sorted(kind.value for kind in SETTLEMENT_KINDS)
)


def _request_to_orm(req: ShotBudgetRequest) -> ShotBudgetRequestModel:
    return ShotBudgetRequestModel(
        entity_id=req.entity_id,
        entity_type=req.ENTITY_TYPE,
        schema_version=req.schema_version,
        entity_version=req.entity_version,
        status=req.status.value,
        owner_module=req.owner_module,
        tenant_id=req.tenant_id,
        created_at=req.created_at,
        created_by=req.created_by,
        updated_at=req.updated_at,
        audit_ref=req.audit_ref,
        request_id=req.request_id,
        project_id=req.project_id,
        shot_id=req.shot_id,
        account_type=req.account_type,
        purpose=req.purpose,
        cost_category=req.cost_category,
        amount_limit=req.amount_limit,
        requested_by=req.requested_by,
        approver=req.approver,
        decision_reason=req.decision_reason,
    )


def _request_to_domain(model: ShotBudgetRequestModel) -> ShotBudgetRequest:
    try:
        status = ShotBudgetRequestStatus(model.status)
    except ValueError as exc:
        raise DomainError(
            ErrorRegistry.INTERNAL_ERROR,
            detail=f"shot_budget_requests 存量枚举非法: status={model.status!r}",
        ) from exc
    return ShotBudgetRequest(
        entity_id=model.entity_id,
        created_by=model.created_by,
        tenant_id=model.tenant_id,
        schema_version=model.schema_version,
        entity_version=model.entity_version,
        owner_module=model.owner_module,
        created_at=model.created_at,
        updated_at=model.updated_at,
        audit_ref=model.audit_ref,
        request_id=model.request_id,
        project_id=model.project_id,
        shot_id=model.shot_id,
        account_type=model.account_type,
        purpose=model.purpose,
        cost_category=model.cost_category,
        amount_limit=model.amount_limit,
        requested_by=model.requested_by,
        approver=model.approver,
        decision_reason=model.decision_reason,
        status=status,
    )


class SqlAlchemyShotBudgetRequestRepository:
    """ShotBudgetRequest 的持久化仓储。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, request: ShotBudgetRequest) -> ShotBudgetRequest:
        self._session.add(_request_to_orm(request))
        await self._session.flush()
        return request

    async def save(
        self, request: ShotBudgetRequest, expected_version: int
    ) -> ShotBudgetRequest:
        await conditional_update(
            self._session,
            ShotBudgetRequestModel,
            request.entity_id,
            expected_version,
            _orm_update_values(_request_to_orm(request)),
        )
        return request

    async def get_by_id(self, entity_id: str) -> ShotBudgetRequest | None:
        result = await self._session.execute(
            select(ShotBudgetRequestModel).where(
                ShotBudgetRequestModel.entity_id == entity_id
            )
        )
        model = result.scalar_one_or_none()
        return _request_to_domain(model) if model is not None else None


def _ledger_to_orm(ledger: BudgetLedger) -> BudgetLedgerModel:
    return BudgetLedgerModel(
        entity_id=ledger.entity_id,
        entity_type=ledger.ENTITY_TYPE,
        schema_version=ledger.schema_version,
        entity_version=ledger.entity_version,
        status=ledger.status.value,
        owner_module=ledger.owner_module,
        tenant_id=ledger.tenant_id,
        created_at=ledger.created_at,
        created_by=ledger.created_by,
        updated_at=ledger.updated_at,
        audit_ref=ledger.audit_ref,
        transaction_id=ledger.transaction_id,
        account_type=ledger.account_type,
        amount=ledger.amount,
        currency=ledger.currency,
        transaction_kind=ledger.transaction_kind,
        idempotency_key=ledger.idempotency_key,
        approval_ref=ledger.approval_ref,
        source=ledger.source,
        reservation_ref=ledger.reservation_ref,
    )


def _ledger_to_domain(model: BudgetLedgerModel) -> BudgetLedger:
    try:
        status = NoLifecycleStatus(model.status)
    except ValueError as exc:
        raise DomainError(
            ErrorRegistry.INTERNAL_ERROR,
            detail=f"budget_ledger 存量枚举非法: status={model.status!r}",
        ) from exc
    return BudgetLedger(
        entity_id=model.entity_id,
        created_by=model.created_by,
        tenant_id=model.tenant_id,
        schema_version=model.schema_version,
        entity_version=model.entity_version,
        owner_module=model.owner_module,
        created_at=model.created_at,
        updated_at=model.updated_at,
        audit_ref=model.audit_ref,
        transaction_id=model.transaction_id,
        account_type=model.account_type,
        amount=model.amount,
        currency=model.currency,
        transaction_kind=model.transaction_kind,
        idempotency_key=model.idempotency_key,
        approval_ref=model.approval_ref,
        source=model.source,
        reservation_ref=model.reservation_ref,
        status=status,
    )


class SqlAlchemyBudgetLedgerRepository:
    """BudgetLedger 的追加式仓储（只 INSERT/SELECT，不可变）。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, ledger: BudgetLedger) -> BudgetLedger:
        """追加一条流水；写失败必须翻译成端口契约异常。

        为什么这里必须翻译而不是让驱动异常冒出去：账本有两条**业务**约束
        （幂等键唯一、同预留同种类唯一），它们都以 ``23505`` 表达。不翻译时调用方
        拿到的是 ``IntegrityError``，最终变成 500——把「你已经释放过这一笔了」
        报成服务器错误，调用方只会重试，而重试永远失败。
        """
        self._session.add(_ledger_to_orm(ledger))
        try:
            await self._session.flush()
        except (IntegrityError, ProgrammingError) as exc:
            translate_write_error(exc, entity_id=ledger.entity_id)
        return ledger

    async def settlement_total(self, *, reservation_ref: str) -> int:
        """某笔预留上**已结算**的额度合计（RELEASE + REFUND）。

        服务层用它做「还可以结算多少」的前置判定。它只给出事实、不做判定：
        真正的闸门在库层（``budget_quota.budget_ledger_settlement_guard``），
        因为「先求和再写入」在两个事务之间必然漏——两边各读到 0，各写一笔满额。
        """
        result = await self._session.execute(
            select(func.coalesce(func.sum(BudgetLedgerModel.amount), 0)).where(
                BudgetLedgerModel.reservation_ref == reservation_ref,
                BudgetLedgerModel.transaction_kind.in_(_SETTLEMENT_KIND_VALUES),
            )
        )
        return int(result.scalar_one())

    async def get_by_id(self, entity_id: str) -> BudgetLedger | None:
        result = await self._session.execute(
            select(BudgetLedgerModel).where(BudgetLedgerModel.entity_id == entity_id)
        )
        model = result.scalar_one_or_none()
        return _ledger_to_domain(model) if model is not None else None

    async def find_settlement(
        self, *, reservation_ref: str, transaction_kind: str
    ) -> BudgetLedger | None:
        """查「这笔预留是否已经被同种结算（RELEASE / REFUND）」过。

        库层有部分唯一索引兜底，但服务层也需要先查：直接撞唯一约束会得到一个
        数据库错误，调用方拿不到「已经释放过」这个业务语义。
        """
        result = await self._session.execute(
            select(BudgetLedgerModel).where(
                BudgetLedgerModel.reservation_ref == reservation_ref,
                BudgetLedgerModel.transaction_kind == transaction_kind,
            )
        )
        model = result.scalar_one_or_none()
        return _ledger_to_domain(model) if model is not None else None


__all__ = [
    "SqlAlchemyBudgetLedgerRepository",
    "SqlAlchemyShotBudgetRequestRepository",
]
