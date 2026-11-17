"""ShotVisualPlan SQLAlchemy 仓储实现（数据所有权：shot_planning 模块专属写入口）。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.shared.persistence import translate_integrity_errors
from packages.shared.versioning import _orm_update_values, conditional_update
from services.shot_planning.domain.enums import ShotVisualPlanStatus
from services.shot_planning.domain.shot_visual_plan import ShotVisualPlan
from services.shot_planning.repository.orm import ShotVisualPlanModel
from services.shot_planning.repository.shot_visual_plan_repo import (
    ShotVisualPlanRepository,
)


def _to_domain(model: ShotVisualPlanModel) -> ShotVisualPlan:
    """ORM -> 领域模型（枚举反序列化防御：非法值降级为结构化 DomainError）。"""
    try:
        status = ShotVisualPlanStatus(model.status)
    except ValueError as exc:
        raise DomainError(
            ErrorRegistry.INTERNAL_ERROR,
            detail=f"shot_visual_plans 存量状态非法: {model.status!r}",
        ) from exc
    return ShotVisualPlan(
        entity_id=model.entity_id,
        shot_id=model.shot_id,
        project_id=model.project_id,
        intent_summary=model.intent_summary,
        camera_language=model.camera_language,
        shot_size=model.shot_size,
        lens_range=model.lens_range,
        motion_intent=model.motion_intent,
        lighting_intent=model.lighting_intent,
        locked_fields=model.locked_fields,
        open_fields=model.open_fields,
        revision=model.revision,
        created_by=model.created_by,
        tenant_id=model.tenant_id,
        organization_id=model.organization_id,
        status=status,
        schema_version=model.schema_version,
        entity_version=model.entity_version,
        owner_module=model.owner_module,
        created_at=model.created_at,
        updated_at=model.updated_at,
        audit_ref=model.audit_ref,
    )


def _to_orm(plan: ShotVisualPlan) -> ShotVisualPlanModel:
    """领域模型 -> ORM。"""
    return ShotVisualPlanModel(
        entity_id=plan.entity_id,
        entity_type=plan.ENTITY_TYPE,
        schema_version=plan.schema_version,
        entity_version=plan.entity_version,
        status=plan.status.value,
        owner_module=plan.owner_module,
        tenant_id=plan.tenant_id,
        organization_id=plan.organization_id,
        created_at=plan.created_at,
        created_by=plan.created_by,
        updated_at=plan.updated_at,
        audit_ref=plan.audit_ref,
        shot_id=plan.shot_id,
        project_id=plan.project_id,
        intent_summary=plan.intent_summary,
        camera_language=plan.camera_language,
        shot_size=plan.shot_size,
        lens_range=plan.lens_range,
        motion_intent=plan.motion_intent,
        lighting_intent=plan.lighting_intent,
        locked_fields=plan.locked_fields,
        open_fields=plan.open_fields,
        revision=plan.revision,
    )


class SqlAlchemyShotVisualPlanRepository(ShotVisualPlanRepository):
    """基于 SQLAlchemy 异步会话的仓储实现。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, plan: ShotVisualPlan) -> None:
        # INSERT 语义 + SAVEPOINT：并发重复插入由主键约束捕获（幂等兜底）。
        with translate_integrity_errors(entity=f"shot_visual_plan_id={plan.entity_id}"):
            async with self._session.begin_nested():
                self._session.add(_to_orm(plan))

    async def save(self, plan: ShotVisualPlan, expected_version: int) -> None:
        # 乐观锁条件更新（状态推进/逐字段锁定）；不 commit。
        await conditional_update(
            self._session,
            ShotVisualPlanModel,
            plan.entity_id,
            expected_version,
            _orm_update_values(_to_orm(plan)),
        )

    async def get_by_id(self, entity_id: str) -> ShotVisualPlan | None:
        stmt = select(ShotVisualPlanModel).where(
            ShotVisualPlanModel.entity_id == entity_id
        )
        result = await self._session.execute(stmt)
        model = result.scalar_one_or_none()
        return _to_domain(model) if model is not None else None
