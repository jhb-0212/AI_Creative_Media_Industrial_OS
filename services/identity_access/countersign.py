"""累积式会签签署服务（ADR-R45 §D3-d，唯一写入点）。

职责
----
把「多主体分次签署、凑齐后放行」收敛为**唯一**服务类：
- ``sign()``   某主体签某席位（角色须 ∈ 该席位候选，fail-closed）；
- ``revoke()`` 签署人撤回（写 REVOKED 新行，不原地改）；
- ``evaluate()`` 读该组事件，调唯一纯函数判定是否凑齐；
- ``consume()`` 凑齐后整组写 CONSUMED + 放行事件（同事务，防重放）。

为什么是唯一写入点
------------------
签署事实的安全不变量（跨席位不同主体、每席位恰好一个、消费不可重放）由
``packages.shared.countersign_rules.evaluate_countersignature`` 判定；写入分散到
多处时，「绕过判定直接插一行 SIGNED」会静默放宽会签。边界断言用 AST 锁
``ApprovalSignatureModel(`` 的构造点白名单——出现第二处即报红。

与 fail-closed rbac 的关系
--------------------------
``rbac.authorize()`` 对多席位动作的单请求**仍然拒绝**（不静默降级）。累积签署
走本服务的显式路径：每席位是单主体请求，用 ``principal_seats`` 校验角色归属；
凑齐后由 ``consume()`` 记录放行。两条路径并存，语义不同，不是放宽。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.identity.principal import Principal
from packages.shared.approval_signature import (
    ApprovalSignatureModel,
    SignatureEvent,
    SignatureEventKind,
)
from packages.shared.countersign_rules import (
    CountersignVerdict,
    SignatureView,
    evaluate_countersignature,
)
from packages.shared.outbox_writer import record_event
from services.identity_access.action_registry import principal_seats


@dataclass(frozen=True)
class SignatureTarget:
    """签署作用对象（资源 + 动作 + 席位）。

    ``action_key`` 决定席位集合（从 Action Registry 派生，不手写）；
    ``resource_type`` / ``resource_id`` 标识被审批的资源。
    """

    action_key: str
    resource_type: str
    resource_id: str


class CountersignatureService:
    """累积会签的唯一写入服务（无状态，持有会话与租户上下文）。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ---------------- 判定 ----------------
    @staticmethod
    def seats_for(action_key: str) -> tuple[frozenset[str], ...]:
        seats = principal_seats(action_key)
        if not seats:
            raise DomainError(
                ErrorRegistry.AUTH_SCOPE_DENIED,
                detail=f"动作 {action_key} 未配置会签席位（契约错误，fail-closed）",
            )
        return seats

    async def _load_events(
        self, *, target: SignatureTarget, tenant_id: UUID
    ) -> list[ApprovalSignatureModel]:
        result = await self._session.execute(
            select(ApprovalSignatureModel).where(
                ApprovalSignatureModel.tenant_id == tenant_id,
                ApprovalSignatureModel.resource_type == target.resource_type,
                ApprovalSignatureModel.resource_id == target.resource_id,
                ApprovalSignatureModel.action_key == target.action_key,
            )
        )
        return list(result.scalars().all())

    async def evaluate(
        self, *, target: SignatureTarget, tenant_id: UUID
    ) -> CountersignVerdict:
        """读取当前签署组，判定是否凑齐（纯判定的唯一调用点之一）。"""
        events = await self._load_events(target=target, tenant_id=tenant_id)
        views = tuple(
            SignatureView(
                seat_index=e.seat_index, subject=e.subject, event_kind=e.event_kind
            )
            for e in events
        )
        return evaluate_countersignature(
            action_key=target.action_key,
            events=views,
            seats=self.seats_for(target.action_key),
        )

    # ---------------- 写入 ----------------
    def _validate_seat_role(
        self, *, action_key: str, seat_index: int, principal: Principal
    ) -> None:
        """签署主体角色须 ∈ 目标席位候选（fail-closed，不靠请求体自报角色）。"""
        seats = self.seats_for(action_key)
        if seat_index < 1 or seat_index > len(seats):
            raise DomainError(
                ErrorRegistry.AUTH_SCOPE_DENIED,
                detail=f"席位 {seat_index} 越界：动作 {action_key} 共 {len(seats)} 席",
            )
        if not (principal.roles & seats[seat_index - 1]):
            raise DomainError(
                ErrorRegistry.AUTH_SCOPE_DENIED,
                detail=f"主体 {principal.subject_id} 缺少席位 {seat_index} 所需角色",
            )
        # 多席位动作里，同一主体不得签两个席位（防「单人签满」降级）。
        # 这里只校验单席位角色；跨席位去重由 evaluate/DB 唯一约束 + 服务在 sign 前
        # 的现有签署检查共同保证，见 sign()。

    async def _write_event(
        self,
        *,
        event: SignatureEvent,
        target: SignatureTarget,
        payload_extra: Mapping[str, Any] | None = None,
    ) -> UUID:
        """写事件 + 签署事实（同事务，event_id 互指）。唯一 ORM 构造点。"""
        event_id = record_event(
            self._session,
            aggregate_type=target.resource_type,
            aggregate_id=target.resource_id,
            tenant_id=event.tenant_id,
            event_type=f"APPROVAL_SIGNATURE_{event.event_kind.value}",
            payload={
                "action_key": target.action_key,
                "seat_index": event.seat_index,
                "subject": event.subject,
                "event_kind": event.event_kind.value,
                "signature_id": str(event.signature_id),
                **(payload_extra or {}),
            },
        )
        self._session.add(
            ApprovalSignatureModel(
                signature_id=event.signature_id,
                tenant_id=event.tenant_id,
                organization_id=event.organization_id,
                resource_type=target.resource_type,
                resource_id=target.resource_id,
                action_key=target.action_key,
                seat_index=event.seat_index,
                subject=event.subject,
                event_kind=event.event_kind.value,
                authority_ref=event.authority_ref,
                evidence_refs=list(event.evidence_refs),
                occurred_at=event.occurred_at,
                event_id=event_id,
            )
        )
        return event_id

    async def sign(
        self,
        *,
        target: SignatureTarget,
        seat_index: int,
        principal: Principal,
        evidence_refs: tuple[str, ...] = (),
    ) -> UUID:
        """某主体签某席位，返回 signature_id。"""
        self._validate_seat_role(
            action_key=target.action_key, seat_index=seat_index, principal=principal
        )
        # 同席位同主体已签（有效）→ 拒绝；同主体签了别的席位 → 拒绝（防单人签满）。
        events = await self._load_events(target=target, tenant_id=principal.tenant_id)
        if any(
            e.seat_index == seat_index
            and e.subject == principal.subject_id
            and e.event_kind == SignatureEventKind.SIGNED.value
            for e in events
        ):
            raise DomainError(
                ErrorRegistry.DUPLICATE_ENTITY,
                detail=f"主体 {principal.subject_id} 已签署席位 {seat_index}",
            )
        if any(
            e.seat_index != seat_index
            and e.subject == principal.subject_id
            and e.event_kind == SignatureEventKind.SIGNED.value
            for e in events
        ):
            raise DomainError(
                ErrorRegistry.AUTH_SCOPE_DENIED,
                detail=(
                    f"主体 {principal.subject_id} 已签署其他席位，"
                    "会签须由不同主体分别签署"
                ),
            )
        event = SignatureEvent(
            tenant_id=principal.tenant_id,
            organization_id=principal.organization_id,
            resource_type=target.resource_type,
            resource_id=target.resource_id,
            action_key=target.action_key,
            seat_index=seat_index,
            subject=principal.subject_id,  # 不可伪：只取 Principal
            event_kind=SignatureEventKind.SIGNED,
            authority_ref=principal.jti,
            evidence_refs=evidence_refs,
        )
        await self._write_event(event=event, target=target)
        return event.signature_id

    async def revoke(
        self, *, target: SignatureTarget, seat_index: int, principal: Principal
    ) -> UUID:
        """签署人撤回自己某席位的签署（只允许撤自己的，写 REVOKED 新行）。"""
        event = SignatureEvent(
            tenant_id=principal.tenant_id,
            organization_id=principal.organization_id,
            resource_type=target.resource_type,
            resource_id=target.resource_id,
            action_key=target.action_key,
            seat_index=seat_index,
            subject=principal.subject_id,
            event_kind=SignatureEventKind.REVOKED,
            authority_ref=principal.jti,
        )
        await self._write_event(event=event, target=target)
        return event.signature_id

    async def consume(
        self, *, target: SignatureTarget, tenant_id: UUID
    ) -> tuple[CountersignVerdict, list[UUID]]:
        """凑齐后整组写 CONSUMED，使签署一次性失效。返回判定与写入的 id 列表。

        未凑齐直接拒绝（fail-closed），不写任何事实。凑齐后为每个有效签署席位写
        一条 CONSUMED（同 seat/subject，不同 event_kind），与放行事件同事务；
        调用方在同一工作单元提交，因此「消费成功但放行失败」不会留下半状态。
        """
        verdict = await self.evaluate(target=target, tenant_id=tenant_id)
        if not verdict.complete:
            raise DomainError(
                ErrorRegistry.AUTH_SCOPE_DENIED,
                detail="会签未凑齐，不得放行：" + "；".join(verdict.problems),
            )
        written: list[UUID] = []
        for seat, subject in verdict.effective.items():
            event = SignatureEvent(
                tenant_id=tenant_id,
                resource_type=target.resource_type,
                resource_id=target.resource_id,
                action_key=target.action_key,
                seat_index=seat,
                subject=subject,
                event_kind=SignatureEventKind.CONSUMED,
            )
            await self._write_event(
                event=event,
                target=target,
                payload_extra={"consumed_seats": len(verdict.effective)},
            )
            written.append(event.signature_id)
        return verdict, written


__all__: list[str] = ["CountersignatureService", "SignatureTarget"]
