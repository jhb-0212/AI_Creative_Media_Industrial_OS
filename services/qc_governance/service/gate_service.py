"""Gate 应用服务（Gate 决策记录，R16-D3）。"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from services.qc_governance.domain.gate_decision import GateDecision


class GateService:
    def __init__(self, repository: Any, outbox_writer: Any) -> None:
        self._repository = repository
        self._outbox_writer = outbox_writer

    async def approve_gate(
        self,
        *,
        gate_key: str,
        project_id: str,
        decision: str,
        decided_by: str,
        tenant_id: UUID,
        gate_instance_id: str = "",
        constraint_snapshot_id: str = "",
        entry_criteria: str = "",
        reason_code: str = "",
        comment: str = "",
        required_artifacts: str = "",
        evidence_refs: list[str] | None = None,
        reviewers: list[str] | None = None,
        ruleset_version: int = 1,
        scope: dict[str, Any] | None = None,
    ) -> GateDecision:
        record = GateDecision(
            entity_id=str(uuid4()),
            created_by=decided_by,
            tenant_id=tenant_id,
            gate_key=gate_key,
            project_id=project_id,
            decision=decision,
            decided_by=decided_by,
            gate_instance_id=gate_instance_id,
            constraint_snapshot_id=constraint_snapshot_id,
            entry_criteria=entry_criteria,
            reason_code=reason_code,
            comment=comment,
            required_artifacts=required_artifacts,
            evidence_refs=evidence_refs or [],
            reviewers=reviewers or [],
            ruleset_version=ruleset_version,
            scope=scope or {},
        )
        await self._repository.add(record)
        self._outbox_writer.record(
            aggregate_type="GateDecision",
            aggregate_id=record.entity_id,
            tenant_id=tenant_id,
            event_type="PRODUCTION_GATE_APPROVED",
            payload={
                "gate_key": record.gate_key,
                "project_id": record.project_id,
                "decision": record.decision,
                "decided_by": decided_by,
                "tenant_id": str(tenant_id),
            },
        )
        return record


__all__ = ["GateService"]
