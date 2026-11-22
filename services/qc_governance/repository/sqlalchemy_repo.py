"""Gate 决策记录的 SQLAlchemy 仓储（R16，project_planning schema 过渡期）。

只提供 ``add`` / ``get_by_id``：决策是追加式不可变记录，领域层连 ``save`` 都没有
（``Entity`` 的版本推进对它无意义）。仓储形状与不变量一致，而不是先给一个通用的
``save`` 再在文档里说「不要用」。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.errors.error_codes import DomainError, ErrorRegistry
from services.project_planning.domain.enums import NoLifecycleStatus
from services.qc_governance.domain.gate_decision import GateDecision
from services.qc_governance.repository.orm import GateDecisionModel


def _to_orm(record: GateDecision) -> GateDecisionModel:
    return GateDecisionModel(
        entity_id=record.entity_id,
        entity_type=record.ENTITY_TYPE,
        schema_version=record.schema_version,
        entity_version=record.entity_version,
        status=record.status.value,
        owner_module=record.owner_module,
        tenant_id=record.tenant_id,
        created_at=record.created_at,
        created_by=record.created_by,
        updated_at=record.updated_at,
        audit_ref=record.audit_ref,
        gate_key=record.gate_key,
        project_id=record.project_id,
        decision=record.decision,
        decided_by=record.decided_by,
        decided_at=record.decided_at,
        gate_instance_id=record.gate_instance_id,
        constraint_snapshot_id=record.constraint_snapshot_id,
        entry_criteria=record.entry_criteria,
        reason_code=record.reason_code,
        comment=record.comment,
        required_artifacts=record.required_artifacts,
        evidence_refs=list(record.evidence_refs),
        reviewers=list(record.reviewers),
        ruleset_version=record.ruleset_version,
        scope=dict(record.scope),
    )


def _to_domain(model: GateDecisionModel) -> GateDecision:
    try:
        status = NoLifecycleStatus(model.status)
    except ValueError as exc:
        raise DomainError(
            ErrorRegistry.INTERNAL_ERROR,
            detail=f"gate_decisions 存量枚举非法: status={model.status!r}",
        ) from exc
    return GateDecision(
        entity_id=model.entity_id,
        created_by=model.created_by,
        tenant_id=model.tenant_id,
        schema_version=model.schema_version,
        entity_version=model.entity_version,
        owner_module=model.owner_module,
        created_at=model.created_at,
        updated_at=model.updated_at,
        audit_ref=model.audit_ref,
        gate_key=model.gate_key,
        project_id=model.project_id,
        decision=model.decision,
        decided_by=model.decided_by,
        decided_at=model.decided_at,
        gate_instance_id=model.gate_instance_id,
        constraint_snapshot_id=model.constraint_snapshot_id,
        entry_criteria=model.entry_criteria,
        reason_code=model.reason_code,
        comment=model.comment,
        required_artifacts=model.required_artifacts,
        evidence_refs=list(model.evidence_refs or []),
        reviewers=list(model.reviewers or []),
        ruleset_version=model.ruleset_version,
        scope=dict(model.scope or {}),
        status=status,
    )


class SqlAlchemyGateDecisionRepository:
    """GateDecision 的追加式仓储（只 INSERT/SELECT，不可变）。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, record: GateDecision) -> GateDecision:
        self._session.add(_to_orm(record))
        # 显式 flush：让 INSERT 的约束失败（如 decision 受控、status 哨兵）在
        # 提交前暴露给调用方的事务，而不是拖到 commit 时变成一句模糊的 500。
        await self._session.flush()
        return record

    async def get_by_id(self, entity_id: str) -> GateDecision | None:
        result = await self._session.execute(
            select(GateDecisionModel).where(GateDecisionModel.entity_id == entity_id)
        )
        model = result.scalar_one_or_none()
        return _to_domain(model) if model is not None else None


__all__ = ["SqlAlchemyGateDecisionRepository"]
