"""审批引擎仓储的 SQLAlchemy 实现（事实 append-only，票据乐观锁）。"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from sqlalchemy import select, update

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.identity.principal import SubjectType
from packages.shared.persistence import translate_integrity_errors
from packages.shared.versioning import ConcurrentModificationError
from services.workflow.domain.approval import (
    ApprovalFlow,
    ApprovalGroup,
    ApprovalStep,
    ApprovalTicket,
)
from services.workflow.domain.enums import ApprovalMode, TicketStatus, VoteDecision
from services.workflow.repository.approval_repo import ApprovalRepository
from services.workflow.repository.orm import (
    ApprovalFlowModel,
    ApprovalStepModel,
    ApprovalTicketModel,
)


def _enum[E: Enum](enum_cls: type[E], value: str, column: str) -> E:
    try:
        return enum_cls(value)
    except ValueError as exc:
        raise DomainError(
            ErrorRegistry.INTERNAL_ERROR,
            detail=f"workflow 存量枚举值非法（{column}）: {value!r}",
        ) from exc


def _flow_from(model: ApprovalFlowModel) -> ApprovalFlow:
    return ApprovalFlow(
        entity_id=model.entity_id,
        action_key=model.action_key,
        resource_type=model.resource_type,
        resource_id=model.resource_id,
        project_id=model.project_id,
        mode=_enum(ApprovalMode, model.mode, "mode"),
        groups=tuple(
            ApprovalGroup(name=str(g["name"]), roles=tuple(g["roles"]))
            for g in model.groups
        ),
        ruleset_version=model.ruleset_version,
        tenant_id=model.tenant_id,
        organization_id=model.organization_id,
        created_by=model.created_by,
        created_at=model.created_at,
        updated_at=model.updated_at,
        status=model.status,
        entity_type=model.entity_type,
        schema_version=model.schema_version,
        entity_version=model.entity_version,
        owner_module=model.owner_module,
        audit_ref=model.audit_ref,
    )


def _ticket_from(model: ApprovalTicketModel) -> ApprovalTicket:
    return ApprovalTicket(
        entity_id=model.entity_id,
        flow_id=model.flow_id,
        project_id=model.project_id,
        tenant_id=model.tenant_id,
        organization_id=model.organization_id,
        status=_enum(TicketStatus, model.status, "status"),
        version=model.version,
        completed_at=model.completed_at,
        created_by=model.created_by,
        created_at=model.created_at,
        updated_at=model.updated_at,
        entity_type=model.entity_type,
        schema_version=model.schema_version,
        entity_version=model.entity_version,
        owner_module=model.owner_module,
        audit_ref=model.audit_ref,
    )


def _step_from(model: ApprovalStepModel) -> ApprovalStep:
    return ApprovalStep(
        entity_id=model.entity_id,
        ticket_id=model.ticket_id,
        group_name=model.group_name,
        voter_subject_id=model.voter_subject_id,
        voter_subject_type=_enum(
            SubjectType, model.voter_subject_type, "voter_subject_type"
        ),
        decision=_enum(VoteDecision, model.decision, "decision"),
        reason=model.reason,
        tenant_id=model.tenant_id,
        organization_id=model.organization_id,
        project_id=model.project_id,
        decided_at=model.decided_at,
        created_by=model.created_by,
        created_at=model.created_at,
        updated_at=model.updated_at,
        entity_type=model.entity_type,
        schema_version=model.schema_version,
        entity_version=model.entity_version,
        status=model.status,
        owner_module=model.owner_module,
        audit_ref=model.audit_ref,
    )


class SqlAlchemyApprovalRepository(ApprovalRepository):
    def __init__(self, session) -> None:  # type: ignore[no-untyped-def]
        self._session = session

    async def add_flow(self, flow: ApprovalFlow) -> None:
        with translate_integrity_errors(entity=f"flow={flow.entity_id}"):
            async with self._session.begin_nested():
                self._session.add(
                    ApprovalFlowModel(
                        entity_id=flow.entity_id,
                        entity_type=flow.entity_type,
                        schema_version=flow.schema_version,
                        entity_version=flow.entity_version,
                        status=flow.status,
                        owner_module=flow.owner_module,
                        tenant_id=flow.tenant_id,
                        organization_id=flow.organization_id,
                        created_at=flow.created_at,
                        created_by=flow.created_by,
                        updated_at=flow.updated_at,
                        audit_ref=flow.audit_ref,
                        action_key=flow.action_key,
                        resource_type=flow.resource_type,
                        resource_id=flow.resource_id,
                        project_id=flow.project_id,
                        mode=flow.mode.value,
                        groups=[
                            {"name": g.name, "roles": list(g.roles)}
                            for g in flow.groups
                        ],
                        ruleset_version=flow.ruleset_version,
                    )
                )

    async def get_flow(self, flow_id: str) -> ApprovalFlow | None:
        result = await self._session.execute(
            select(ApprovalFlowModel).where(ApprovalFlowModel.entity_id == flow_id)
        )
        model = result.scalar_one_or_none()
        return _flow_from(model) if model else None

    async def add_ticket(self, ticket: ApprovalTicket) -> None:
        with translate_integrity_errors(entity=f"ticket={ticket.entity_id}"):
            async with self._session.begin_nested():
                self._session.add(
                    ApprovalTicketModel(
                        entity_id=ticket.entity_id,
                        entity_type=ticket.entity_type,
                        schema_version=ticket.schema_version,
                        entity_version=ticket.entity_version,
                        status=ticket.status.value,
                        owner_module=ticket.owner_module,
                        tenant_id=ticket.tenant_id,
                        organization_id=ticket.organization_id,
                        created_at=ticket.created_at,
                        created_by=ticket.created_by,
                        updated_at=ticket.updated_at,
                        audit_ref=ticket.audit_ref,
                        flow_id=ticket.flow_id,
                        project_id=ticket.project_id,
                        version=ticket.version,
                        completed_at=ticket.completed_at,
                    )
                )

    async def get_ticket(self, ticket_id: str) -> ApprovalTicket | None:
        result = await self._session.execute(
            select(ApprovalTicketModel).where(
                ApprovalTicketModel.entity_id == ticket_id
            )
        )
        model = result.scalar_one_or_none()
        return _ticket_from(model) if model else None

    async def save_ticket(
        self, ticket: ApprovalTicket, *, expected_version: int
    ) -> None:
        next_version = expected_version + 1
        now: datetime = ticket.updated_at
        result = await self._session.execute(
            update(ApprovalTicketModel)
            .where(
                ApprovalTicketModel.entity_id == ticket.entity_id,
                ApprovalTicketModel.version == expected_version,
            )
            .values(
                status=ticket.status.value,
                version=next_version,
                completed_at=ticket.completed_at,
                updated_at=now,
                audit_ref=ticket.audit_ref,
            )
        )
        if result.rowcount != 1:
            raise ConcurrentModificationError(
                f"approval_ticket:{ticket.entity_id}", expected_version
            )
        ticket.version = next_version

    async def add_step(self, step: ApprovalStep) -> None:
        with translate_integrity_errors(entity=f"step={step.entity_id}"):
            async with self._session.begin_nested():
                self._session.add(
                    ApprovalStepModel(
                        entity_id=step.entity_id,
                        entity_type=step.entity_type,
                        schema_version=step.schema_version,
                        entity_version=step.entity_version,
                        status=step.status,
                        owner_module=step.owner_module,
                        tenant_id=step.tenant_id,
                        organization_id=step.organization_id,
                        created_at=step.created_at,
                        created_by=step.created_by,
                        updated_at=step.updated_at,
                        audit_ref=step.audit_ref,
                        ticket_id=step.ticket_id,
                        group_name=step.group_name,
                        voter_subject_id=step.voter_subject_id,
                        voter_subject_type=step.voter_subject_type.value,
                        decision=step.decision.value,
                        reason=step.reason,
                        decided_at=step.decided_at,
                        project_id=step.project_id,
                    )
                )

    async def list_steps(self, ticket_id: str) -> tuple[ApprovalStep, ...]:
        result = await self._session.execute(
            select(ApprovalStepModel)
            .where(ApprovalStepModel.ticket_id == ticket_id)
            .order_by(ApprovalStepModel.decided_at, ApprovalStepModel.entity_id)
        )
        return tuple(_step_from(model) for model in result.scalars().all())

    async def find_step(
        self, *, ticket_id: str, group_name: str, voter_subject_id: str
    ) -> ApprovalStep | None:
        result = await self._session.execute(
            select(ApprovalStepModel).where(
                ApprovalStepModel.ticket_id == ticket_id,
                ApprovalStepModel.group_name == group_name,
                ApprovalStepModel.voter_subject_id == voter_subject_id,
            )
        )
        model = result.scalar_one_or_none()
        return _step_from(model) if model else None


__all__ = ["PROBE_FIELDS", "SqlAlchemyApprovalRepository"]

#: 序列化探针样本（契约校验使用；见 scripts/contract_check.py）。
PROBE_FIELDS = {
    "entity_id": "probe-ticket",
    "flow_id": "probe-flow",
    "completed_at": None,
    "version": 1,
}
