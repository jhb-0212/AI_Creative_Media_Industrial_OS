"""ShotSpec SQLAlchemy 仓储实现（数据所有权：shot_planning 模块专属写入口）。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.shared.persistence import translate_integrity_errors
from packages.shared.versioning import _orm_update_values, conditional_update
from services.shot_planning.domain.enums import ShotSpecStatus
from services.shot_planning.domain.shot_spec import ShotSpec
from services.shot_planning.repository.orm import ShotSpecModel
from services.shot_planning.repository.shot_spec_repo import ShotSpecRepository


def _to_domain(model: ShotSpecModel) -> ShotSpec:
    """ORM -> 领域模型（枚举反序列化防御：非法值降级为结构化 DomainError）。"""
    try:
        status = ShotSpecStatus(model.status)
    except ValueError as exc:
        raise DomainError(
            ErrorRegistry.INTERNAL_ERROR,
            detail=f"shot_specs 存量状态非法: {model.status!r}",
        ) from exc
    return ShotSpec(
        entity_id=model.entity_id,
        shot_revision_id=model.shot_revision_id,
        shot_id=model.shot_id,
        project_id=model.project_id,
        duration_ms=model.duration_ms,
        aspect_ratio=model.aspect_ratio,
        fps=model.fps,
        coordinate_system=model.coordinate_system,
        camera_path=model.camera_path,
        subject_paths=model.subject_paths,
        shot_size_changes=model.shot_size_changes,
        cut_points_frames=model.cut_points_frames,
        previz_ref=model.previz_ref,
        control_signal_refs=model.control_signal_refs,
        locked_dimensions=model.locked_dimensions,
        locked_by=model.locked_by,
        locked_at=model.locked_at,
        lock_rationale=model.lock_rationale,
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


def _to_orm(spec: ShotSpec) -> ShotSpecModel:
    """领域模型 -> ORM。"""
    return ShotSpecModel(
        entity_id=spec.entity_id,
        entity_type=spec.ENTITY_TYPE,
        schema_version=spec.schema_version,
        entity_version=spec.entity_version,
        status=spec.status.value,
        owner_module=spec.owner_module,
        tenant_id=spec.tenant_id,
        organization_id=spec.organization_id,
        created_at=spec.created_at,
        created_by=spec.created_by,
        updated_at=spec.updated_at,
        audit_ref=spec.audit_ref,
        shot_revision_id=spec.shot_revision_id,
        shot_id=spec.shot_id,
        project_id=spec.project_id,
        duration_ms=spec.duration_ms,
        aspect_ratio=spec.aspect_ratio,
        fps=spec.fps,
        coordinate_system=spec.coordinate_system,
        camera_path=spec.camera_path,
        subject_paths=spec.subject_paths,
        shot_size_changes=spec.shot_size_changes,
        cut_points_frames=spec.cut_points_frames,
        previz_ref=spec.previz_ref,
        control_signal_refs=spec.control_signal_refs,
        locked_dimensions=spec.locked_dimensions,
        locked_by=spec.locked_by,
        locked_at=spec.locked_at,
        lock_rationale=spec.lock_rationale,
    )


class SqlAlchemyShotSpecRepository(ShotSpecRepository):
    """基于 SQLAlchemy 异步会话的仓储实现。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, spec: ShotSpec) -> None:
        # INSERT 语义 + SAVEPOINT：并发重复插入由主键约束捕获（幂等兜底）。
        with translate_integrity_errors(entity=f"shot_spec_id={spec.entity_id}"):
            async with self._session.begin_nested():
                self._session.add(_to_orm(spec))

    async def save(self, spec: ShotSpec, expected_version: int) -> None:
        # 乐观锁条件更新（仅锁定推进路径）；不 commit。
        await conditional_update(
            self._session,
            ShotSpecModel,
            spec.entity_id,
            expected_version,
            _orm_update_values(_to_orm(spec)),
        )

    async def get_by_id(self, entity_id: str) -> ShotSpec | None:
        stmt = select(ShotSpecModel).where(ShotSpecModel.entity_id == entity_id)
        result = await self._session.execute(stmt)
        model = result.scalar_one_or_none()
        return _to_domain(model) if model is not None else None
