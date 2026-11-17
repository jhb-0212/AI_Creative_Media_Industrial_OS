"""通用审批引擎用例层单测（内存仓储，无数据库）。

覆盖用户点名的负向场景与并发口径（ADR-R11-G）：

- 越权发起审批（角色不符 / 目标动作无绑定 / 契约原文漂移）；
- 无权限投票（角色不在主体上界 / 角色不在分组 / Agent / 发起人自投）；
- 重复投票幂等 vs 内容改写冲突；
- 并发驳回：两路都算出 REJECTED，但乐观锁只允许一路推进票据，
  **完成事件只发一次**；
- 完成事件携带正确处理结论与投票轨迹（业务侧据此推进自身状态）。
"""

from __future__ import annotations

import uuid
from dataclasses import replace

import pytest

from packages.approval_bindings import ApprovalGroupBinding
from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.errors.repository_errors import DuplicateEntityError
from packages.identity.principal import Principal, SubjectType
from packages.shared.versioning import ConcurrentModificationError
from services.workflow.domain.approval import (
    ApprovalFlow,
    ApprovalStep,
    ApprovalTicket,
)
from services.workflow.domain.enums import ApprovalMode, TicketStatus, VoteDecision
from services.workflow.domain.ports import ResourceOwnership
from services.workflow.repository.approval_repo import ApprovalRepository
from services.workflow.service.approval_service import ApprovalService
from tests import make_test_capability
from tests.unit.test_asset_service import FakeOutboxWriter

_TENANT = uuid.UUID("00000000-0000-0000-0000-00000000d5b1")
_PROJECT = "proj-approval-service"
_TARGET = "DELIVERY.MANIFEST.APPROVE"


def _principal(
    subject: str,
    *roles: str,
    project_ids: frozenset[str] = frozenset({"*"}),
    subject_type: SubjectType = SubjectType.HUMAN,
) -> Principal:
    return Principal(
        subject_id=subject,
        tenant_id=_TENANT,
        organization_id=_TENANT,
        roles=frozenset(roles),
        project_ids=project_ids,
        subject_type=subject_type,
        capability=make_test_capability(subject, _TENANT),
    )


_INITIATOR = _principal("initiator", "制片", "交付负责人")
_OWNER = _principal("owner", "交付负责人")
_QC = _principal("qc", "QC Worker")


class FakeApprovalRepository(ApprovalRepository):
    def __init__(self) -> None:
        self.flows: dict[str, ApprovalFlow] = {}
        self.tickets: dict[str, ApprovalTicket] = {}
        self.steps: list[ApprovalStep] = []
        self.stale_ticket: ApprovalTicket | None = None
        self.hide_first_step = False
        self._hidden_used = False

    async def add_flow(self, flow: ApprovalFlow) -> None:
        if flow.entity_id in self.flows:
            raise DuplicateEntityError(flow.entity_id)
        self.flows[flow.entity_id] = replace(flow)

    async def get_flow(self, flow_id: str) -> ApprovalFlow | None:
        flow = self.flows.get(flow_id)
        return replace(flow) if flow else None

    async def add_ticket(self, ticket: ApprovalTicket) -> None:
        if ticket.entity_id in self.tickets:
            raise DuplicateEntityError(ticket.entity_id)
        self.tickets[ticket.entity_id] = replace(ticket)

    async def get_ticket(self, ticket_id: str) -> ApprovalTicket | None:
        if self.stale_ticket is not None:
            return replace(self.stale_ticket)
        ticket = self.tickets.get(ticket_id)
        return replace(ticket) if ticket else None

    async def save_ticket(
        self, ticket: ApprovalTicket, *, expected_version: int
    ) -> None:
        # 与 SqlAlchemyApprovalRepository.save_ticket 同口径：版本由仓储推进，
        # 不匹配即冲突。fake 不模拟这一步，乐观锁测试就会假绿。
        current = self.tickets.get(ticket.entity_id)
        if current is None or current.version != expected_version:
            raise ConcurrentModificationError(ticket.entity_id, expected_version)
        advanced = replace(ticket, version=expected_version + 1)
        self.tickets[ticket.entity_id] = advanced

    async def add_step(self, step: ApprovalStep) -> None:
        existing = await self.find_step(
            ticket_id=step.ticket_id,
            group_name=step.group_name,
            voter_subject_id=step.voter_subject_id,
        )
        if existing is not None:
            raise DuplicateEntityError(step.entity_id)
        self.steps.append(replace(step))

    async def list_steps(self, ticket_id: str) -> tuple[ApprovalStep, ...]:
        return tuple(replace(s) for s in self.steps if s.ticket_id == ticket_id)

    async def find_step(
        self, *, ticket_id: str, group_name: str, voter_subject_id: str
    ) -> ApprovalStep | None:
        if self.hide_first_step and not self._hidden_used:
            self._hidden_used = True
            return None
        for step in self.steps:
            if (
                step.ticket_id == ticket_id
                and step.group_name == group_name
                and step.voter_subject_id == voter_subject_id
            ):
                return replace(step)
        return None


class FakeOwnershipPort:
    """按 (resource_type, resource_id) 返回归属结论的测试桩。

    ``unknown`` 集合里的资源视为不存在；``mismatches`` 里的资源归属
    与请求项目不同的项目（用于跨项目拒绝测试）；未登记的类型视为
    无解析器（fail-closed 测试）。
    """

    def __init__(self) -> None:
        self.ownership: dict[tuple[str, str], str] = {}
        self.unknown: set[tuple[str, str]] = set()
        self.unresolvable_types: set[str] = set()

    def can_resolve(self, resource_type: str) -> bool:
        return resource_type not in self.unresolvable_types

    async def resolve(
        self,
        *,
        tenant_id: object,
        organization_id: object,
        resource_type: str,
        resource_id: str,
    ) -> ResourceOwnership | None:
        key = (resource_type, resource_id)
        if key in self.unknown:
            return None
        project = self.ownership.get(key, _PROJECT)
        return ResourceOwnership(project_id=project)


def _service(repo: ApprovalRepository | None = None):
    store = repo or FakeApprovalRepository()
    outbox = FakeOutboxWriter()
    ownership = FakeOwnershipPort()
    return ApprovalService(store, outbox, ownership), store, outbox, ownership


def _start_fields(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "target_action_key": _TARGET,
        "resource_type": "DeliveryManifest",
        "resource_id": "manifest-1",
        "project_id": _PROJECT,
        "ruleset_version": 3,
        "principal": _INITIATOR,
    }
    values.update(overrides)
    return values


async def _start(service: ApprovalService, **overrides: object) -> ApprovalTicket:
    return await service.start(**_start_fields(**overrides))  # type: ignore[arg-type]


def _vote_fields(ticket_id: str, principal: Principal, **overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "ticket_id": ticket_id,
        "group_name": "delivery_owner",
        "decision": VoteDecision.APPROVE,
        "reason": "同意",
        "principal": principal,
    }
    values.update(overrides)
    return values


def _events(outbox: FakeOutboxWriter, event_type: str) -> list[dict[str, object]]:
    return [e for e in outbox.events if e["event_type"] == event_type]


# --- 发起审批 ----------------------------------------------------------------


async def test_start_creates_flow_ticket_and_event_from_principal() -> None:
    """发起票据：身份来自 Principal，状态 PENDING，发 CREATED 事件。"""
    service, store, outbox, _ownership = _service()
    ticket = await _start(service, entity_id="ticket-1")

    assert ticket.status is TicketStatus.PENDING
    assert ticket.created_by == _INITIATOR.subject_id
    assert ticket.version == 1
    flow_id = store.tickets["ticket-1"].flow_id
    assert flow_id in store.flows
    flow = store.flows[flow_id]
    assert flow.action_key == _TARGET
    assert flow.mode is ApprovalMode.ALL
    assert [g.name for g in flow.groups] == ["delivery_owner", "qc_rights"]

    created = _events(outbox, "APPROVAL_TICKET_CREATED")
    assert len(created) == 1
    assert created[0]["aggregate_type"] == "ApprovalTicket"
    assert created[0]["aggregate_id"] == "ticket-1"
    assert created[0]["payload"]["action_key"] == _TARGET
    assert "decision" not in created[0]["payload"]  # 发起事件不含结论


async def test_start_rejects_unbound_action() -> None:
    """目标动作没有审批绑定：fail-closed 拒绝，不落票据。"""
    service, store, outbox, _ownership = _service()
    with pytest.raises(DomainError) as exc:
        await _start(service, target_action_key="SCRIPT.IMPORT")
    assert exc.value.error_code is ErrorRegistry.VALIDATION_ERROR
    assert not store.tickets
    assert not outbox.events


async def test_start_rejects_role_outside_initiator_scope() -> None:
    """越权发起：编剧不在 START_APPROVAL 允许主体内。"""
    service, store, outbox, _ownership = _service()
    with pytest.raises(DomainError) as exc:
        await _start(service, principal=_principal("writer", "编剧"))
    assert exc.value.error_code is ErrorRegistry.AUTH_SCOPE_DENIED
    assert not store.tickets
    assert not outbox.events


async def test_start_rejects_project_out_of_scope() -> None:
    """越权发起：项目不在令牌授权范围。"""
    service, store, _, _ownership = _service()
    narrow = _principal("producer", "制片", project_ids=frozenset({"proj-other"}))
    with pytest.raises(DomainError) as exc:
        await _start(service, principal=narrow)
    assert exc.value.error_code is ErrorRegistry.AUTH_SCOPE_DENIED
    assert not store.tickets


async def test_start_rejects_when_binding_text_drifts_from_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """台账原文与契约漂移：拒绝按旧分组发起（与 principal_bindings 同口径）。"""
    drifted = ApprovalBindingStub()
    monkeypatch.setattr(
        "services.workflow.service.approval_service.binding_for",
        lambda _key: drifted,
    )
    service, store, outbox, _ownership = _service()
    with pytest.raises(DomainError) as exc:
        await _start(service)
    assert exc.value.error_code is ErrorRegistry.INTERNAL_ERROR
    assert not store.tickets
    assert not outbox.events


async def test_start_rejects_phase_gated_action() -> None:
    """R4-2：目标动作 PHASE_GATED（未开放）→ 拒绝发起票据，不落票据。"""
    service, store, outbox, _ownership = _service()
    with pytest.raises(DomainError) as exc:
        await _start(
            service,
            target_action_key="COMMUNITY.REUSE.APPROVE",
            resource_type="ReuseRequest",
            resource_id="reuse-1",
        )
    assert exc.value.error_code is ErrorRegistry.PHASE_NOT_ENABLED
    assert not store.tickets
    assert not outbox.events


async def test_start_rejects_resource_type_mismatch() -> None:
    """R4-2：请求的 resource_type 与目标动作契约不符 → 拒绝。"""
    service, store, outbox, _ownership = _service()
    with pytest.raises(DomainError) as exc:
        await _start(
            service,
            target_action_key=_TARGET,
            resource_type="NotTheRightType",
        )
    assert exc.value.error_code is ErrorRegistry.VALIDATION_ERROR
    assert not store.tickets
    assert not outbox.events


class ApprovalBindingStub:
    """契约原文已漂移的绑定桩（contract_text 故意写错）。"""

    action_key = _TARGET
    contract_text = "已漂移的原文"
    mode = "ALL"
    groups = (
        ApprovalGroupBinding("delivery_owner", frozenset({"交付负责人"})),
        ApprovalGroupBinding("qc_rights", frozenset({"QC Worker"})),
    )
    reason = "测试桩"
    unblocks_when = "测试桩"


# --- 投票与授权 --------------------------------------------------------------


async def test_start_rejects_resource_missing() -> None:
    """目标资源不存在（租户内查无此资源）：拒绝为幽灵资源发票据。"""
    service, _, _, ownership = _service()
    ownership.unknown.add(("DeliveryManifest", "manifest-ghost"))
    with pytest.raises(DomainError) as exc_info:
        await _start(service, resource_id="manifest-ghost")
    assert exc_info.value.error_code.code == "NOT_FOUND"


async def test_start_rejects_cross_project_resource() -> None:
    """目标资源属于别的项目：归属与票据声明不一致即拒绝（R4-2 收口）。"""
    service, _, _, ownership = _service()
    ownership.ownership[("DeliveryManifest", "manifest-1")] = "proj-other"
    with pytest.raises(DomainError) as exc_info:
        await _start(service)
    assert exc_info.value.error_code.code == "VALIDATION_ERROR"
    assert "proj-other" in str(exc_info.value.detail)


async def test_start_rejects_unresolvable_resource_type() -> None:
    """资源类型没有归属解析器：fail-closed，不盲发票据。"""
    service, _, _, ownership = _service()
    ownership.unresolvable_types.add("DeliveryManifest")
    with pytest.raises(DomainError) as exc_info:
        await _start(service)
    assert exc_info.value.error_code.code == "VALIDATION_ERROR"
    assert "归属解析器" in str(exc_info.value.detail)


async def test_start_passes_when_resource_owned_by_ticket_project() -> None:
    """目标资源存在且归属票据项目：正常建票（归属校验不误伤主链）。"""
    service, _, outbox, ownership = _service()
    ownership.ownership[("DeliveryManifest", "manifest-1")] = _PROJECT
    ticket = await _start(service, entity_id="ticket-owned")
    assert ticket.status.value == "PENDING"
    assert len(_events(outbox, "APPROVAL_TICKET_CREATED")) == 1


async def test_two_groups_approve_completes_ticket_once() -> None:
    """ALL：两组都赞成 → 票据 APPROVED，完成事件只发一次。"""
    service, store, outbox, _ownership = _service()
    await _start(service, entity_id="ticket-1")

    await service.submit_vote(**_vote_fields("ticket-1", _OWNER, entity_id="step-1"))
    assert store.tickets["ticket-1"].status is TicketStatus.PENDING
    assert not _events(outbox, "APPROVAL_TICKET_COMPLETED")

    await service.submit_vote(
        **_vote_fields(
            "ticket-1", _QC, group_name="qc_rights", entity_id="step-2"
        )
    )
    assert store.tickets["ticket-1"].status is TicketStatus.APPROVED
    # 只有终结那一票推进版本（1→2）；第一票未终结不改票据。
    assert store.tickets["ticket-1"].version == 2

    completed = _events(outbox, "APPROVAL_TICKET_COMPLETED")
    assert len(completed) == 1
    payload = completed[0]["payload"]
    assert payload["decision"] == "APPROVED"
    assert payload["action_key"] == _TARGET
    assert payload["resource_id"] == "manifest-1"
    assert {v["group_name"] for v in payload["votes"]} == {"delivery_owner", "qc_rights"}


async def test_reject_completes_ticket_immediately() -> None:
    """ALL：一组否决即终结，不必等另一组。"""
    service, store, outbox, _ownership = _service()
    await _start(service, entity_id="ticket-1")
    await service.submit_vote(
        **_vote_fields(
            "ticket-1",
            _OWNER,
            decision=VoteDecision.REJECT,
            reason="权利证据不足",
            entity_id="step-1",
        )
    )
    assert store.tickets["ticket-1"].status is TicketStatus.REJECTED
    completed = _events(outbox, "APPROVAL_TICKET_COMPLETED")
    assert len(completed) == 1
    assert completed[0]["payload"]["decision"] == "REJECTED"


async def test_vote_rejects_principal_outside_allowed_upper_bound() -> None:
    """无权限投票：角色不在 WORKFLOW.SUBMIT_VOTE 主体上界。"""
    service, store, outbox, _ownership = _service()
    await _start(service, entity_id="ticket-1")
    writer = _principal("writer", "编剧")
    with pytest.raises(DomainError) as exc:
        await service.submit_vote(**_vote_fields("ticket-1", writer))
    assert exc.value.error_code is ErrorRegistry.AUTH_SCOPE_DENIED
    assert not store.steps
    assert not _events(outbox, "APPROVAL_TICKET_COMPLETED")


async def test_vote_rejects_principal_outside_group() -> None:
    """无权限投票：主体上界通过，但角色不在目标分组。"""
    service, store, _, _ownership = _service()
    await _start(service, entity_id="ticket-1")
    # 版权在主体上界内，但 delivery_owner 分组只接受交付负责人。
    rights = _principal("rights", "版权")
    with pytest.raises(DomainError) as exc:
        await service.submit_vote(**_vote_fields("ticket-1", rights))
    assert exc.value.error_code is ErrorRegistry.AUTH_SCOPE_DENIED
    assert not store.steps


async def test_vote_rejects_agent() -> None:
    """Agent 投票：CRITICAL 动作由授权层拒绝（领域与数据库还有两道）。"""
    service, store, _, _ownership = _service()
    await _start(service, entity_id="ticket-1")
    agent = _principal(
        "agent-1", "交付负责人", subject_type=SubjectType.AGENT
    )
    with pytest.raises(DomainError) as exc:
        await service.submit_vote(**_vote_fields("ticket-1", agent))
    assert exc.value.error_code is ErrorRegistry.AUTH_SCOPE_DENIED
    assert not store.steps


async def test_initiator_cannot_vote_on_own_ticket() -> None:
    """发起人不得投自己的票：职责分离，身份来自票据创建人。"""
    service, store, _, _ownership = _service()
    await _start(service, entity_id="ticket-1")
    with pytest.raises(DomainError) as exc:
        await service.submit_vote(**_vote_fields("ticket-1", _INITIATOR))
    assert exc.value.error_code is ErrorRegistry.AUTH_SCOPE_DENIED
    assert not store.steps


async def test_terminal_ticket_rejects_vote() -> None:
    """终态票据不可再投票（结论不可改写）。"""
    service, store, outbox, _ownership = _service()
    await _start(service, entity_id="ticket-1")
    await service.submit_vote(
        **_vote_fields(
            "ticket-1",
            _OWNER,
            decision=VoteDecision.REJECT,
            reason="不合格",
            entity_id="step-1",
        )
    )
    with pytest.raises(DomainError) as exc:
        await service.submit_vote(
            **_vote_fields("ticket-1", _QC, group_name="qc_rights")
        )
    assert exc.value.error_code is ErrorRegistry.STATE_TRANSITION_DENIED
    assert len(store.steps) == 1
    assert len(_events(outbox, "APPROVAL_TICKET_COMPLETED")) == 1


async def test_unknown_group_is_rejected() -> None:
    service, store, _, _ownership = _service()
    await _start(service, entity_id="ticket-1")
    with pytest.raises(DomainError) as exc:
        await service.submit_vote(
            **_vote_fields("ticket-1", _OWNER, group_name="not_a_group")
        )
    assert exc.value.error_code is ErrorRegistry.VALIDATION_ERROR
    assert not store.steps


# --- 幂等与并发 --------------------------------------------------------------


async def test_repeat_same_vote_is_idempotent() -> None:
    """同主体同组重复提交同一票：返回既有选票，不重复发事件。"""
    service, store, outbox, _ownership = _service()
    await _start(service, entity_id="ticket-1")
    first = await service.submit_vote(
        **_vote_fields("ticket-1", _OWNER, entity_id="step-1")
    )
    again = await service.submit_vote(
        **_vote_fields("ticket-1", _OWNER, entity_id="step-2")
    )
    assert again.entity_id == first.entity_id == "step-1"
    assert len(store.steps) == 1
    assert len(_events(outbox, "APPROVAL_VOTE_SUBMITTED")) == 1


async def test_repeat_vote_with_different_content_is_conflict() -> None:
    """改写已投的票：409，选票不可改写。"""
    service, store, outbox, _ownership = _service()
    await _start(service, entity_id="ticket-1")
    await service.submit_vote(**_vote_fields("ticket-1", _OWNER, entity_id="step-1"))
    with pytest.raises(DomainError) as exc:
        await service.submit_vote(
            **_vote_fields(
                "ticket-1",
                _OWNER,
                decision=VoteDecision.REJECT,
                reason="反悔",
                entity_id="step-2",
            )
        )
    assert exc.value.error_code is ErrorRegistry.IDEMPOTENCY_CONFLICT
    assert len(store.steps) == 1
    assert not _events(outbox, "APPROVAL_TICKET_COMPLETED")


async def test_concurrent_duplicate_vote_is_idempotent() -> None:
    """唯一键冲突 + 回查命中同内容：幂等返回，不重复发事件。"""
    repo = FakeApprovalRepository()
    repo.hide_first_step = True
    service, store, outbox, _ownership = _service(repo)
    await _start(service, entity_id="ticket-1")
    # 先落一张选票，再让下一次 find_step 首次返回 None、add_step 抛重复。
    await service.submit_vote(**_vote_fields("ticket-1", _OWNER, entity_id="step-1"))
    repo.hide_first_step = True
    result = await service.submit_vote(
        **_vote_fields("ticket-1", _OWNER, entity_id="step-2")
    )
    assert result.entity_id == "step-1"
    assert len(store.steps) == 1
    assert len(_events(outbox, "APPROVAL_VOTE_SUBMITTED")) == 1


async def test_concurrent_reject_emits_completed_once() -> None:
    """并发驳回：两路都读到 PENDING，但只有抢到乐观锁的一路推进并发完成事件。"""
    repo = FakeApprovalRepository()
    service, store, outbox, _ownership = _service(repo)
    ticket = await _start(service, entity_id="ticket-1")
    # 固定旧快照：两路投票都认为自己读到的票据还是 v1/PENDING。
    repo.stale_ticket = replace(ticket)

    await service.submit_vote(
        **_vote_fields(
            "ticket-1",
            _OWNER,
            decision=VoteDecision.REJECT,
            reason="交付负责人否决",
            entity_id="step-1",
        )
    )
    with pytest.raises(DomainError) as exc:
        await service.submit_vote(
            **_vote_fields(
                "ticket-1",
                _QC,
                group_name="qc_rights",
                decision=VoteDecision.REJECT,
                reason="QC 否决",
                entity_id="step-2",
            )
        )
    assert exc.value.error_code is ErrorRegistry.CONCURRENT_MODIFICATION
    assert store.tickets["ticket-1"].status is TicketStatus.REJECTED
    assert store.tickets["ticket-1"].version == 2
    assert len(_events(outbox, "APPROVAL_TICKET_COMPLETED")) == 1
    # 两路选票事实都写入（都真实发生），但完成结论只发一次。
    assert len(store.steps) == 2


async def test_get_missing_ticket_is_not_found() -> None:
    service, _, _, _ = _service()
    with pytest.raises(DomainError) as exc:
        await service.get_ticket("missing")
    assert exc.value.error_code is ErrorRegistry.NOT_FOUND
