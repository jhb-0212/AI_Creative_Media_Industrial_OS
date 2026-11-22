"""Budget 应用服务（单镜头预算申请与审批 + 账本原子记账，R16-D3）。"""

from __future__ import annotations

from typing import Any, Literal, cast
from uuid import UUID, uuid4

from packages.errors.error_codes import DomainError, ErrorRegistry
from services.budget_quota.domain.budget_ledger import BudgetLedger
from services.budget_quota.domain.shot_budget_request import ShotBudgetRequest
from services.project_planning.domain.enums import ShotBudgetRequestStatus
from services.project_planning.domain.state_machine import BUDGET_SHOT_APPROVE_ACTION
from services.project_planning.service.transition import advance_status


class BudgetService:
    def __init__(self, repository: Any, ledger_repository: Any, outbox_writer: Any) -> None:
        self._repository = repository
        self._ledger_repository = ledger_repository
        self._outbox_writer = outbox_writer

    async def request_budget(
        self,
        *,
        project_id: str,
        shot_id: str,
        account_type: str,
        purpose: str,
        cost_category: str,
        amount_limit: int,
        requested_by: str,
        tenant_id: UUID,
        request_id: str = "",
    ) -> ShotBudgetRequest:
        request = ShotBudgetRequest(
            entity_id=str(uuid4()),
            created_by=requested_by,
            tenant_id=tenant_id,
            request_id=request_id,
            project_id=project_id,
            shot_id=shot_id,
            account_type=account_type,
            purpose=purpose,
            cost_category=cost_category,
            amount_limit=amount_limit,
            requested_by=requested_by,
        )
        await self._repository.add(request)
        self._outbox_writer.record(
            aggregate_type="ShotBudgetRequest",
            aggregate_id=request.entity_id,
            tenant_id=tenant_id,
            event_type="SHOT_BUDGET_REQUESTED",
            payload={
                "request_id": request.request_id,
                "project_id": request.project_id,
                "shot_id": request.shot_id,
                "account_type": request.account_type,
                "amount_limit": request.amount_limit,
                "requested_by": requested_by,
                "tenant_id": str(tenant_id),
            },
        )
        return request

    async def approve_budget(
        self,
        *,
        entity_id: str,
        decision: Literal["APPROVED", "REJECTED"],
        approver: str,
        decision_reason: str,
        tenant_id: UUID,
    ) -> ShotBudgetRequest:
        request = await self._repository.get_by_id(entity_id)
        if request is None:
            raise DomainError(
                ErrorRegistry.NOT_FOUND, detail=f"预算申请不存在: {entity_id}"
            )

        # 审批要素在推进前落位：transition_to(APPROVED) 校验 approver 非空且与
        # 申请人职责分离；transition_to(REJECTED) 校验 decision_reason 非空。
        request.approver = approver
        request.decision_reason = decision_reason

        target = ShotBudgetRequestStatus(decision)
        await advance_status(
            request,
            target,
            repository=self._repository,
            outbox_writer=self._outbox_writer,
            aggregate_type="ShotBudgetRequest",
            event_type="SHOT_BUDGET_APPROVED",
            tenant_id=tenant_id,
            payload={
                "request_id": request.request_id,
                "project_id": request.project_id,
                "shot_id": request.shot_id,
                "decision": decision,
                "approver": approver,
                "tenant_id": str(tenant_id),
            },
            resource_type="ShotBudgetRequest",
            owner_action=BUDGET_SHOT_APPROVE_ACTION,
            conflict_detail="预算申请并发冲突，请基于最新状态重试",
            actor_field="approver",
            actor=approver,
        )

        # APPROVED 时原子追加预算账本流水（幂等键防重复入账）。
        if decision == "APPROVED":
            ledger = BudgetLedger(
                entity_id=str(uuid4()),
                created_by=approver,
                tenant_id=tenant_id,
                transaction_id="",
                account_type=request.account_type,
                amount=request.amount_limit,
                currency="CNY",
                transaction_kind="RESERVE",
                idempotency_key=f"shot-budget:{request.request_id}",
                approval_ref=request.entity_id,
                source=request.entity_id,
            )
            await self._ledger_repository.add(ledger)
        return cast("ShotBudgetRequest", request)

    # ---- Phase 2 §2：账本释放 / 冲销 ------------------------------------- #

    async def _settle(
        self,
        *,
        kind: Literal["RELEASE", "REFUND"],
        reservation_ledger_id: str,
        amount: int,
        actor: str,
        tenant_id: UUID,
        reason: str,
    ) -> BudgetLedger:
        run = await self._ledger_repository.get_by_id(reservation_ledger_id)
        if run is None:
            raise DomainError(
                ErrorRegistry.NOT_FOUND,
                detail=f"预留言本不存在: {reservation_ledger_id}",
            )
        # 只能对 RESERVE 流水做结算：对一条 RELEASE 再 RELEASE 会让余额无限下探。
        if run.transaction_kind != "RESERVE":
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail=f"{kind} 只能作用于 RESERVE 流水，实际为 {run.transaction_kind}",
            )
        if run.tenant_id != tenant_id:
            raise DomainError(
                ErrorRegistry.AUTH_SCOPE_DENIED,
                detail="预留言本不属于当前租户",
            )
        if amount <= 0 or amount > run.amount:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail=f"{kind} 金额必须大于 0 且不超过预留金额 {run.amount}，实际 {amount}",
            )
        existing = await self._ledger_repository.find_settlement(
            reservation_ref=run.entity_id, transaction_kind=kind
        )
        if existing is not None:
            raise DomainError(
                ErrorRegistry.IDEMPOTENCY_CONFLICT,
                detail=f"该预留已存在 {kind} 流水（{existing.entity_id}），不可重复结算",
            )

        ledger = BudgetLedger(
            entity_id=str(uuid4()),
            created_by=actor,
            tenant_id=tenant_id,
            transaction_id="",
            account_type=run.account_type,
            amount=amount,
            currency=run.currency,
            transaction_kind=kind,
            idempotency_key=f"budget-{kind.lower()}:::{run.entity_id}",
            approval_ref=run.approval_ref,
            source=run.entity_id,
            reservation_ref=run.entity_id,
        )
        await self._ledger_repository.add(ledger)
        payload = {
            "reservation_ref": run.entity_id,
            "transaction_kind": kind,
            "amount": amount,
            "idempotency_key": ledger.idempotency_key,
            "actor": actor,
            "reason": reason,
            "tenant_id": str(tenant_id),
        }
        # 事件名必须写字面量：事件契约靠源码字面量双向核对（登记了必须真发），
        # 用三元表达式拼名字会让「把 SHOT_BUDGET_RELEASED 错写成 ..._REFUNDED」查不出来。
        if kind == "RELEASE":
            self._outbox_writer.record(
                aggregate_type="BudgetLedger",
                aggregate_id=ledger.entity_id,
                tenant_id=tenant_id,
                event_type="SHOT_BUDGET_RELEASED",
                payload=payload,
            )
        else:
            self._outbox_writer.record(
                aggregate_type="BudgetLedger",
                aggregate_id=ledger.entity_id,
                tenant_id=tenant_id,
                event_type="SHOT_BUDGET_REFUNDED",
                payload=payload,
            )
        return ledger

    async def release_budget(
        self,
        *,
        reservation_ledger_id: str,
        amount: int,
        actor: str,
        tenant_id: UUID,
        reason: str = "",
    ) -> BudgetLedger:
        """释放已预留但未消耗的额度（新增 RELEASE 流水，不改旧流水）。"""
        return await self._settle(
            kind="RELEASE",
            reservation_ledger_id=reservation_ledger_id,
            amount=amount,
            actor=actor,
            tenant_id=tenant_id,
            reason=reason,
        )

    async def refund_budget(
        self,
        *,
        reservation_ledger_id: str,
        amount: int,
        actor: str,
        tenant_id: UUID,
        reason: str = "",
    ) -> BudgetLedger:
        """对已结算额度做冲销（新增 REFUND 流水，减法由消费者按 kind 计算）。"""
        return await self._settle(
            kind="REFUND",
            reservation_ledger_id=reservation_ledger_id,
            amount=amount,
            actor=actor,
            tenant_id=tenant_id,
            reason=reason,
        )


__all__ = ["BudgetService"]
