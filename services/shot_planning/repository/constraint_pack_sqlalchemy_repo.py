"""ConstraintPack SQLAlchemy 仓储实现（数据所有权：shot_planning 模块专属写入口）。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.shared.persistence import translate_integrity_errors
from packages.shared.versioning import _orm_update_values, conditional_update
from services.shot_planning.domain.constraint_pack import ConstraintPack
from services.shot_planning.domain.enums import (
    ConstraintPackApplicability,
    ConstraintPackStatus,
)
from services.shot_planning.repository.constraint_pack_repo import (
    ConstraintPackRepository,
)
from services.shot_planning.repository.orm import ConstraintPackModel


def _to_domain(model: ConstraintPackModel) -> ConstraintPack:
    """ORM -> 领域模型（枚举反序列化防御：非法值降级为结构化 DomainError）。"""
    try:
        status = ConstraintPackStatus(model.status)
        applicability = ConstraintPackApplicability(model.applicability)
    except ValueError as exc:
        raise DomainError(
            ErrorRegistry.INTERNAL_ERROR,
            detail=(
                f"constraint_packs 存量取值非法: status={model.status!r} "
                f"applicability={model.applicability!r}"
            ),
        ) from exc
    return ConstraintPack(
        entity_id=model.entity_id,
        project_id=model.project_id,
        scene_id=model.scene_id,
        shot_ids=model.shot_ids,
        shot_group_ids=model.shot_group_ids,
        applicability=applicability,
        locked_dimensions=model.locked_dimensions,
        constraints=model.constraints,
        source_previz_revision=model.source_previz_revision,
        version=model.version,
        approved_by=model.approved_by,
        approved_at=model.approved_at,
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


def _to_orm(pack: ConstraintPack) -> ConstraintPackModel:
    """领域模型 -> ORM。"""
    return ConstraintPackModel(
        entity_id=pack.entity_id,
        entity_type=pack.ENTITY_TYPE,
        schema_version=pack.schema_version,
        entity_version=pack.entity_version,
        status=pack.status.value,
        owner_module=pack.owner_module,
        tenant_id=pack.tenant_id,
        organization_id=pack.organization_id,
        created_at=pack.created_at,
        created_by=pack.created_by,
        updated_at=pack.updated_at,
        audit_ref=pack.audit_ref,
        project_id=pack.project_id,
        scene_id=pack.scene_id,
        shot_ids=pack.shot_ids,
        shot_group_ids=pack.shot_group_ids,
        applicability=pack.applicability.value,
        locked_dimensions=pack.locked_dimensions,
        constraints=pack.constraints,
        source_previz_revision=pack.source_previz_revision,
        version=pack.version,
        approved_by=pack.approved_by,
        approved_at=pack.approved_at,
    )


class SqlAlchemyConstraintPackRepository(ConstraintPackRepository):
    """基于 SQLAlchemy 异步会话的仓储实现。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, pack: ConstraintPack) -> None:
        # INSERT 语义 + SAVEPOINT：并发重复插入由主键约束捕获（幂等兜底）。
        with translate_integrity_errors(entity=f"constraint_pack_id={pack.entity_id}"):
            async with self._session.begin_nested():
                self._session.add(_to_orm(pack))

    async def save(self, pack: ConstraintPack, expected_version: int) -> None:
        # 乐观锁条件更新（仅状态推进）；不 commit。
        await conditional_update(
            self._session,
            ConstraintPackModel,
            pack.entity_id,
            expected_version,
            _orm_update_values(_to_orm(pack)),
        )

    async def get_by_id(self, entity_id: str) -> ConstraintPack | None:
        stmt = select(ConstraintPackModel).where(
            ConstraintPackModel.entity_id == entity_id
        )
        result = await self._session.execute(stmt)
        model = result.scalar_one_or_none()
        return _to_domain(model) if model is not None else None
