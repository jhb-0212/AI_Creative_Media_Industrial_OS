"""Budget 应用服务（单镜头预算申请与审批 + 账本原子记账，R16-D3）。"""

from __future__ import annotations

from typing import Any, Literal, cast
from uuid import UUID, uuid4

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.shared.port_errors import DuplicateEntityError, ReferentialIntegrityError
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
            raise DomainError(ErrorRegistry.NOT_FOUND, detail=f"预算申请不存在: {entity_id}")

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
            try:
                await self._ledger_repository.add(ledger)
            except DuplicateEntityError as exc:
                # 幂等键已存在＝这笔审批已经入过账。这是业务冲突，不是 500：
                # 让调用方看到「已入账」才能停止重试。
                raise DomainError(
                    ErrorRegistry.IDEMPOTENCY_CONFLICT,
                    detail=(
                        f"预算申请 {request.request_id} 已入账"
                        f"（幂等键 shot-budget:{request.request_id}）"
                    ),
                ) from exc
            except ReferentialIntegrityError as exc:
                raise DomainError(
                    ErrorRegistry.VALIDATION_ERROR,
                    detail=f"预留流水被账本约束拒绝（申请 {request.request_id}）",
                ) from exc
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
        if amount <= 0:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail=f"{kind} 金额必须大于 0，实际 {amount}",
            )
        # 先判「同种结算是否已存在」：重复结算的正确语义是「你已经释放过了」，
        # 而不是「这次金额太大」——后者会把幂等冲突误导成金额问题。
        existing = await self._ledger_repository.find_settlement(
            reservation_ref=run.entity_id, transaction_kind=kind
        )
        if existing is not None:
            raise DomainError(
                ErrorRegistry.IDEMPOTENCY_CONFLICT,
                detail=f"该预留已存在 {kind} 流水（{existing.entity_id}），不可重复结算",
            )
        # 再以「剩余可结算额度」为界，而不是「不超过预留金额」。
        #
        # 后者是逐笔判定，挡不住「一个 RELEASE 拿满额、再来一个 REFUND 又拿满额」：
        # 两笔各自都 ≤ 预留，合计却是 2 倍。账面上的后果是同一笔钱被退两次。
        # 这里的求和只是**提前给出可读的拒绝理由**；权威闸门在库层触发器
        # （``budget_quota.budget_ledger_settlement_guard``），因为两个事务各读
        # 一次「已结算 0」时，服务层的求和必然同时放过两笔。
        settled = await self._ledger_repository.settlement_total(reservation_ref=run.entity_id)
        if settled + amount > run.amount:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail=(
                    f"{kind} 后累计结算 {settled + amount} 将超过预留 {run.amount}"
                    f"（已结算 {settled}，本次 {amount}）：一笔预留的 RELEASE+REFUND "
                    "合计不得超过 RESERVE"
                ),
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
        await self._write_ledger(ledger, kind=kind, reservation_ref=run.entity_id)
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

    async def _write_ledger(self, ledger: BudgetLedger, *, kind: str, reservation_ref: str) -> None:
        """追加流水，把库层拒绝翻译成**业务**语义。

        三种拒绝必须分开报，否则调用方无法判断该重试、该换金额、还是该报缺陷：

        - ``DuplicateEntityError``（23505，部分唯一索引）＝ 并发下同一预留的同类
          结算被写了两笔。语义同「已经结算过」→ 幂等冲突，不重试；
        - ``ReferentialIntegrityError``（23514，结算守卫）＝ 累计结算超过预留，
          或预留言本不存在。语义是「取值/引用不成立」→ 参数问题，不重试；
        - 其余异常原样上抛（部署缺陷、连接中断等，冒到 500 才是对的）。
        """
        try:
            await self._ledger_repository.add(ledger)
        except DuplicateEntityError as exc:
            raise DomainError(
                ErrorRegistry.IDEMPOTENCY_CONFLICT,
                detail=f"该预留已存在 {kind} 流水（并发重复结算被库层拒绝）",
            ) from exc
        except ReferentialIntegrityError as exc:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail=(
                    f"{kind} 被账本不变量拒绝（预留 {reservation_ref}）："
                    "累计 RELEASE+REFUND 不得超过该笔 RESERVE 金额，"
                    "且结算必须指向存在的预留"
                ),
            ) from exc

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
