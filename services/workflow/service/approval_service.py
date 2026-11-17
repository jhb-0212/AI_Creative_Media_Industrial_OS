"""通用审批引擎用例层（票据状态机 + 投票事实 + Outbox 事件）。

职责边界：
- 这里决定「谁有资格投哪一票、什么条件下结论成立」，不决定业务对象如何改状态；
- 业务侧只消费 `APPROVAL_TICKET_COMPLETED`，不调用本服务读结论（R2 禁止跨服务直连）；
- 授权：动作级用统一入口 `authorize`，分组资格用审批绑定的角色集合，
  两者都过才算有票权；发起人也不得投自己发起的票。

并发口径：投票事实的唯一键兜住重复投票；票据状态用乐观锁推进，冲突时
**不自动合并**——第二个并发请求收到 409，由调用方基于最新票据重试。
结论事件与票据完成在同一事务里发出，且只在第一次终结时发。
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import datetime

from packages.approval_bindings import binding_for
from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.errors.repository_errors import DuplicateEntityError
from packages.identity.authorization import ResourceRef
from packages.identity.enforcement import authorize
from packages.identity.principal import Principal
from packages.shared.entity import utcnow
from packages.shared.outbox_writer import OutboxWriter
from packages.shared.versioning import ConcurrentModificationError
from services.workflow.domain.approval import (
    ApprovalFlow,
    ApprovalGroup,
    ApprovalStep,
    ApprovalTicket,
    evaluate,
)
from services.workflow.domain.enums import ApprovalMode, TicketStatus, VoteDecision
from services.workflow.domain.ports import ResourceOwnershipPort
from services.workflow.domain.state_machine import assert_transition
from services.workflow.repository.approval_repo import ApprovalRepository


class ApprovalService:
    def __init__(
        self,
        repo: ApprovalRepository,
        outbox: OutboxWriter,
        ownership: ResourceOwnershipPort,
    ) -> None:
        self._repo = repo
        self._outbox = outbox
        # 跨域归属端口：组合根注入（apps/api），workflow 只消费归属结论。
        # 必填——没有它就退回"盲发票据"，R4-2 缺口重新打开。
        self._ownership = ownership

    async def start(
        self,
        *,
        target_action_key: str,
        resource_type: str,
        resource_id: str,
        project_id: str,
        ruleset_version: int,
        principal: Principal,
        entity_id: str | None = None,
    ) -> ApprovalTicket:
        """发起审批票据（发起人不等于审批人）。"""
        binding = binding_for(target_action_key)
        # 台账里的 contract_text 必须与当前契约原文一致：不一致说明契约改了，
        # 不能继续按旧分组授权（与 principal_bindings 的 fail-fast 同口径）。
        from packages.identity.action_registry import action_spec

        spec = action_spec(target_action_key)
        if spec is None or spec.allowed_principals_text != binding.contract_text:
            raise DomainError(
                ErrorRegistry.INTERNAL_ERROR,
                detail=(
                    f"动作 {target_action_key} 的审批绑定原文与契约不一致，"
                    "拒绝按旧分组发起审批"
                ),
            )

        # R4-2：为目标动作状态与资源类型把关，拒绝"为尚未开放的动作建票据"
        # 与"用 A 动作的绑定去审批 B 类型的资源"。
        if spec.is_phase_gated:
            raise DomainError(
                ErrorRegistry.PHASE_NOT_ENABLED,
                detail=(
                    f"动作 {target_action_key} 当前状态为 {spec.status!r}，"
                    "尚未开放，不得发起审批票据"
                ),
            )
        if spec.resource_type != resource_type:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail=(
                    f"目标动作 {target_action_key} 的契约资源类型是 "
                    f"{spec.resource_type!r}，与请求的 {resource_type!r} 不一致"
                ),
            )

        # R4-2 收口：目标资源必须真实存在且属于票据声明的项目。
        # 归属解析经跨域只读端口（组合根适配），workflow 角色不直读他域表。
        if not self._ownership.can_resolve(resource_type):
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail=(
                    f"资源类型 {resource_type!r} 没有跨域归属解析器，"
                    "无法证明资源存在且属于本项目，拒绝盲发审批票据"
                ),
            )
        ownership = await self._ownership.resolve(
            tenant_id=principal.tenant_id,
            organization_id=principal.organization_id,
            resource_type=resource_type,
            resource_id=resource_id,
        )
        if ownership is None:
            raise DomainError(
                ErrorRegistry.NOT_FOUND,
                detail=(
                    f"目标资源 {resource_type}/{resource_id} 在租户内不存在，"
                    "拒绝为不存在的资源发起审批"
                ),
            )
        if ownership.project_id != project_id:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail=(
                    f"目标资源 {resource_type}/{resource_id} 归属项目 "
                    f"{ownership.project_id!r}，与票据声明项目 {project_id!r} "
                    "不一致，拒绝跨项目发起审批"
                ),
            )

        authorize(
            principal,
            "WORKFLOW.START_APPROVAL",
            resource=ResourceRef(
                resource_type="ApprovalTicket",
                organization_id=principal.organization_id,
                project_id=project_id,
            ),
        )

        flow_id = f"flow-{uuid.uuid4().hex}"
        ticket_id = entity_id or f"ticket-{uuid.uuid4().hex}"
        now = utcnow()
        flow = ApprovalFlow(
            entity_id=flow_id,
            action_key=target_action_key,
            resource_type=resource_type,
            resource_id=resource_id,
            project_id=project_id,
            mode=ApprovalMode(binding.mode),
            groups=tuple(
                ApprovalGroup(name=g.name, roles=tuple(sorted(g.roles)))
                for g in binding.groups
            ),
            ruleset_version=ruleset_version,
            tenant_id=principal.tenant_id,
            organization_id=principal.organization_id,
            created_by=principal.subject_id,
            created_at=now,
            updated_at=now,
        )
        ticket = ApprovalTicket(
            entity_id=ticket_id,
            flow_id=flow_id,
            project_id=project_id,
            tenant_id=principal.tenant_id,
            organization_id=principal.organization_id,
            status=TicketStatus.PENDING,
            version=1,
            created_by=principal.subject_id,
            created_at=now,
            updated_at=now,
        )
        await self._repo.add_flow(flow)
        await self._repo.add_ticket(ticket)
        self._outbox.record(
            aggregate_type="ApprovalTicket",
            aggregate_id=ticket_id,
            tenant_id=principal.tenant_id,
            event_type="APPROVAL_TICKET_CREATED",
            payload={
                "ticket_id": ticket_id,
                "flow_id": flow_id,
                "action_key": target_action_key,
                "resource_id": resource_id,
                "mode": flow.mode.value,
                "groups": [g.name for g in flow.groups],
                "tenant_id": str(principal.tenant_id),
            },
        )
        return ticket

    async def submit_vote(
        self,
        *,
        ticket_id: str,
        group_name: str,
        decision: VoteDecision,
        reason: str,
        principal: Principal,
        entity_id: str | None = None,
    ) -> ApprovalStep:
        """投一票；结论成立时同事务推进票据并发完成事件。"""
        ticket = await self._repo.get_ticket(ticket_id)
        if ticket is None:
            raise DomainError(
                ErrorRegistry.NOT_FOUND, detail=f"审批票据不存在: {ticket_id}"
            )
        if ticket.status is not TicketStatus.PENDING:
            raise DomainError(
                ErrorRegistry.STATE_TRANSITION_DENIED,
                detail=f"审批票据已处于终态 {ticket.status.value}，不得再投票",
            )

        authorize(
            principal,
            "WORKFLOW.SUBMIT_VOTE",
            resource=ResourceRef(
                resource_type="ApprovalTicket",
                organization_id=ticket.organization_id,
                project_id=ticket.project_id,
            ),
        )

        flow = await self._repo.get_flow(ticket.flow_id)
        if flow is None:
            raise DomainError(
                ErrorRegistry.INTERNAL_ERROR,
                detail=f"审批票据 {ticket_id} 的关联流程不存在（数据损坏）",
            )
        group = flow.group(group_name)
        if group is None:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR,
                detail=f"分组 {group_name} 不属于该审批流程",
            )
        if not group.accepts(principal):
            raise DomainError(
                ErrorRegistry.AUTH_SCOPE_DENIED,
                detail=f"主体不具备分组 {group_name} 的投票资格",
            )
        if principal.subject_id == ticket.created_by:
            # 职责分离：发起人不等于审批人。这是"谁提请的"唯一可信来源
            # （票据创建人来自 Principal，不由调用方声明）。
            raise DomainError(
                ErrorRegistry.AUTH_SCOPE_DENIED,
                detail="审批发起人不得投自己发起的票据（职责分离）",
            )

        step_id = entity_id or f"step-{uuid.uuid4().hex}"
        existing = await self._repo.find_step(
            ticket_id=ticket_id,
            group_name=group_name,
            voter_subject_id=principal.subject_id,
        )
        if existing is not None:
            if (
                existing.decision is decision
                and existing.reason.strip() == reason.strip()
            ):
                return existing
            raise DomainError(
                ErrorRegistry.IDEMPOTENCY_CONFLICT,
                detail="同一主体在同一分组已有不同结论的选票；选票不可改写",
            )

        step = ApprovalStep(
            entity_id=step_id,
            ticket_id=ticket_id,
            group_name=group_name,
            voter_subject_id=principal.subject_id,
            voter_subject_type=principal.subject_type,
            decision=decision,
            reason=reason,
            tenant_id=ticket.tenant_id,
            organization_id=ticket.organization_id,
            project_id=ticket.project_id,
            decided_at=utcnow(),
            created_by=principal.subject_id,
        )
        try:
            await self._repo.add_step(step)
        except DuplicateEntityError as exc:
            # 并发重复投票：回查后按快速路径同一规则判定。
            concurrent = await self._repo.find_step(
                ticket_id=ticket_id,
                group_name=group_name,
                voter_subject_id=principal.subject_id,
            )
            if concurrent is not None and (
                concurrent.decision is decision
                and concurrent.reason.strip() == reason.strip()
            ):
                return concurrent
            raise DomainError(
                ErrorRegistry.IDEMPOTENCY_CONFLICT,
                detail="并发投票与既有选票内容不同；选票不可改写",
            ) from exc

        self._outbox.record(
            aggregate_type="ApprovalTicket",
            aggregate_id=ticket_id,
            tenant_id=ticket.tenant_id,
            event_type="APPROVAL_VOTE_SUBMITTED",
            payload={
                "ticket_id": ticket_id,
                "step_id": step_id,
                "group_name": group_name,
                "voter_subject_id": principal.subject_id,
                "decision": decision.value,
                "tenant_id": str(ticket.tenant_id),
            },
        )

        steps = await self._repo.list_steps(ticket_id)
        status = evaluate(flow.mode, flow.groups, steps)
        if status is None:
            return step

        assert_transition(TicketStatus.PENDING, status)
        completed_at: datetime = utcnow()
        completed = replace(
            ticket,
            status=status,
            completed_at=completed_at,
            updated_at=completed_at,
            audit_ref=step_id,
        )
        try:
            await self._repo.save_ticket(completed, expected_version=ticket.version)
        except ConcurrentModificationError as exc:
            # 另一路并发已终结票据：不重复发完成事件，报可重试冲突。
            raise DomainError(
                ErrorRegistry.CONCURRENT_MODIFICATION,
                detail=f"审批票据 {ticket_id} 已被并发投票推进，请重试",
            ) from exc

        self._outbox.record(
            aggregate_type="ApprovalTicket",
            aggregate_id=ticket_id,
            tenant_id=ticket.tenant_id,
            event_type="APPROVAL_TICKET_COMPLETED",
            payload={
                "ticket_id": ticket_id,
                "flow_id": flow.entity_id,
                "action_key": flow.action_key,
                "resource_type": flow.resource_type,
                "resource_id": flow.resource_id,
                # R4-2：事件必须携带项目归属，消费端才能比对目标资源的项目，
                # 反驳"在项目 A 发起、指向项目 B 资源"的跨项目审批链。
                "project_id": flow.project_id,
                "decision": status.value,
                "votes": [
                    {
                        "group_name": s.group_name,
                        "voter_subject_id": s.voter_subject_id,
                        "decision": s.decision.value,
                    }
                    for s in steps
                ],
                "tenant_id": str(ticket.tenant_id),
            },
        )
        return step


    async def get_ticket(self, ticket_id: str) -> ApprovalTicket:
        ticket = await self._repo.get_ticket(ticket_id)
        if ticket is None:
            raise DomainError(
                ErrorRegistry.NOT_FOUND, detail=f"审批票据不存在: {ticket_id}"
            )
        return ticket


__all__ = ["ApprovalService"]
