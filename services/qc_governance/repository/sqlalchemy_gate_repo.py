"""GateDecision 的 SQLAlchemy 仓储实现（append-only，无更新路径）。"""

from __future__ import annotations

from enum import Enum

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.identity.principal import SubjectType
from packages.shared.persistence import translate_integrity_errors
from services.qc_governance.domain.enums import (
    GateDecisionStatus,
    GateDecisionValue,
    GateKey,
)
from services.qc_governance.domain.gate_decision import ENTITY_TYPE, GateDecision
from services.qc_governance.repository.gate_repo import GateDecisionRepository
from services.qc_governance.repository.orm import GateDecisionModel


def _require_enum[E: Enum](enum_cls: type[E], value: str, column: str) -> E:
    """存量字符串 -> 枚举（非法值转结构化错误，不静默丢弃）。

    读到本实现不认识的值说明契约与代码漂移了；静默跳过会让这一类记录
    **从审计视野里消失**，比报错危险得多。
    """
    try:
        return enum_cls(value)
    except ValueError as exc:
        raise DomainError(
            ErrorRegistry.INTERNAL_ERROR,
            detail=f"gate_decisions 存量枚举值非法（{column}）: {value!r}",
        ) from exc


def _to_domain(model: GateDecisionModel) -> GateDecision:
    return GateDecision(
        entity_id=model.entity_id,
        project_id=model.project_id,
        gate_key=_require_enum(GateKey, model.gate_key, "gate_key"),
        gate_instance_id=model.gate_instance_id,
        decision=_require_enum(
            GateDecisionValue, model.decision, "decision"
        ),
        constraint_snapshot_id=model.constraint_snapshot_id,
        ruleset_version=model.ruleset_version,
        entry_criteria=model.entry_criteria,
        required_artifacts=model.required_artifacts,
        reviewers=[str(v) for v in model.reviewers],
        reason_code=model.reason_code,
        comment=model.comment,
        evidence_refs=[str(v) for v in model.evidence_refs],
        scope=dict(model.scope),
        decided_by=model.decided_by,
        decided_by_subject_type=_require_enum(
            SubjectType, model.decided_by_subject_type, "decided_by_subject_type"
        ),
        created_by=model.created_by,
        tenant_id=model.tenant_id,
        organization_id=model.organization_id,
        decided_at=model.decided_at,
        status=_require_enum(GateDecisionStatus, model.status, "status"),
        schema_version=model.schema_version,
        entity_version=model.entity_version,
        owner_module=model.owner_module,
        created_at=model.created_at,
        updated_at=model.updated_at,
        audit_ref=model.audit_ref,
    )


def _to_orm(decision: GateDecision) -> GateDecisionModel:
    return GateDecisionModel(
        entity_id=decision.entity_id,
        entity_type=ENTITY_TYPE,
        schema_version=decision.schema_version,
        entity_version=decision.entity_version,
        status=decision.status.value,
        owner_module=decision.owner_module,
        tenant_id=decision.tenant_id,
        organization_id=decision.organization_id,
        created_at=decision.created_at,
        created_by=decision.created_by,
        updated_at=decision.updated_at,
        audit_ref=decision.audit_ref,
        project_id=decision.project_id,
        gate_key=decision.gate_key.value,
        gate_instance_id=decision.gate_instance_id,
        decision=decision.decision.value,
        constraint_snapshot_id=decision.constraint_snapshot_id,
        ruleset_version=decision.ruleset_version,
        entry_criteria=decision.entry_criteria,
        required_artifacts=decision.required_artifacts,
        reviewers=list(decision.reviewers),
        reason_code=decision.reason_code,
        comment=decision.comment,
        evidence_refs=list(decision.evidence_refs),
        scope=dict(decision.scope),
        decided_by=decision.decided_by,
        decided_by_subject_type=decision.decided_by_subject_type.value,
        decided_at=decision.decided_at,
    )


class SqlAlchemyGateDecisionRepository(GateDecisionRepository):
    """基于 SQLAlchemy 异步会话的仓储实现。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, decision: GateDecision) -> None:
        # 主键冲突与幂等键冲突都会被数据库拒绝（后者是并发下两条相同裁决同时
        # 插入），统一翻译成 DuplicateEntityError，由服务层决定幂等返回还是拒绝。
        with translate_integrity_errors(entity=f"entity_id={decision.entity_id}"):
            async with self._session.begin_nested():
                self._session.add(_to_orm(decision))

    async def get_by_id(self, entity_id: str) -> GateDecision | None:
        stmt = select(GateDecisionModel).where(
            GateDecisionModel.entity_id == entity_id
        )
        result = await self._session.execute(stmt)
        model = result.scalar_one_or_none()
        return _to_domain(model) if model is not None else None

    async def find_by_idempotency_key(
        self,
        *,
        gate_instance_id: str,
        constraint_snapshot_id: str,
        ruleset_version: int,
        decision: str,
    ) -> GateDecision | None:
        stmt = select(GateDecisionModel).where(
            GateDecisionModel.gate_instance_id == gate_instance_id,
            GateDecisionModel.constraint_snapshot_id == constraint_snapshot_id,
            GateDecisionModel.ruleset_version == ruleset_version,
            GateDecisionModel.decision == decision,
        )
        result = await self._session.execute(stmt)
        model = result.scalar_one_or_none()
        return _to_domain(model) if model is not None else None

    async def list_by_instance(self, gate_instance_id: str) -> list[GateDecision]:
        stmt = (
            select(GateDecisionModel)
            .where(GateDecisionModel.gate_instance_id == gate_instance_id)
            .order_by(GateDecisionModel.decided_at, GateDecisionModel.entity_id)
        )
        result = await self._session.execute(stmt)
        return [_to_domain(model) for model in result.scalars().all()]


#: 契约一致性探针的样本字段（供 scripts/contract_check.py 构造合法实例）。
PROBE_FIELDS = {
    "entity_id": "probe-gate-decision",
    "project_id": "probe-project",
    "gate_key": "GATE_4",
    "gate_instance_id": "probe-gate-instance",
    "decision": "APPROVED",
    "constraint_snapshot_id": "probe-snapshot",
    "ruleset_version": 1,
    "entry_criteria": "probe-entry-criteria",
    "required_artifacts": "probe-required-artifacts",
    "reviewers": ["probe"],
    "reason_code": "PROBE",
    "comment": "probe-comment",
    "evidence_refs": ["probe-evidence"],
    "scope": {"level": "project"},
    "decided_by": "probe",
    "created_by": "probe",
}

__all__ = ["PROBE_FIELDS", "SqlAlchemyGateDecisionRepository"]
