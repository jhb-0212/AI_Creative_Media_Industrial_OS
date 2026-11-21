"""镜头域 SQLAlchemy 仓储（持久化映射只在端口实现里出现）。

领域层不知道 JSONB、复合外键与乐观锁 SQL；仓储不知道业务规则。这样
「契约字段 → 领域字段 → ORM 列」只有一条映射链，任何一侧加字段都会在这条链上
显式失败，而不是靠数据库默认值把缺字段补成半个事实。
"""

from __future__ import annotations

from enum import Enum
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.shared.port_errors import translate_write_error
from services.project_planning.domain.enums import ShotRevisionStatus, ShotStatus
from services.project_planning.domain.shot import Shot
from services.project_planning.domain.shot_revision import ShotRevision
from services.project_planning.repository._versioning import (
    _orm_update_values,
    conditional_update,
)
from services.project_planning.repository.orm import ShotModel, ShotRevisionModel
from services.project_planning.repository.shot_repo import (
    ReworkTaskVerifier,
    ShotRepository,
    ShotRevisionRepository,
)


def _enum[EnumT: Enum](
    value: str, enum_cls: type[EnumT], entity: str, column: str
) -> EnumT:
    try:
        return enum_cls(value)
    except ValueError as exc:
        raise DomainError(
            ErrorRegistry.INTERNAL_ERROR,
            detail=f"{entity} 存量 {column} 非法: {value!r}",
        ) from exc


def _shot_from_orm(model: ShotModel) -> Shot:
    return Shot(
        entity_id=model.entity_id,
        created_by=model.created_by,
        tenant_id=model.tenant_id,
        status=_enum(model.status, ShotStatus, "shots", "status"),
        schema_version=model.schema_version,
        entity_version=model.entity_version,
        owner_module=model.owner_module,
        created_at=model.created_at,
        updated_at=model.updated_at,
        audit_ref=model.audit_ref,
        shot_code=model.shot_code,
        scene_id=model.scene_id,
        project_id=model.project_id,
        shot_revision_id=model.shot_revision_id,
        blocked_reason=model.blocked_reason,
        rework_basis_ref=model.rework_basis_ref,
        cancellation_ref=model.cancellation_ref,
        cancelled_at=model.cancelled_at,
        source_draft_ref=model.source_draft_ref,
        organization_id=model.organization_id,
    )


def _shot_to_orm(shot: Shot) -> ShotModel:
    return ShotModel(
        entity_id=shot.entity_id,
        entity_type=shot.ENTITY_TYPE,
        schema_version=shot.schema_version,
        entity_version=shot.entity_version,
        status=shot.status.value,
        owner_module=shot.owner_module,
        tenant_id=shot.tenant_id,
        created_at=shot.created_at,
        created_by=shot.created_by,
        updated_at=shot.updated_at,
        audit_ref=shot.audit_ref,
        shot_id=shot.shot_id,
        shot_code=shot.shot_code,
        scene_id=shot.scene_id,
        project_id=shot.project_id,
        shot_revision_id=shot.shot_revision_id,
        blocked_reason=shot.blocked_reason,
        rework_basis_ref=shot.rework_basis_ref,
        cancellation_ref=shot.cancellation_ref,
        cancelled_at=shot.cancelled_at,
        source_draft_ref=shot.source_draft_ref,
        organization_id=shot.organization_id,
    )


def _revision_from_orm(model: ShotRevisionModel) -> ShotRevision:
    return ShotRevision(
        entity_id=model.entity_id,
        created_by=model.created_by,
        tenant_id=model.tenant_id,
        status=_enum(model.status, ShotRevisionStatus, "shot_revisions", "status"),
        schema_version=model.schema_version,
        entity_version=model.entity_version,
        owner_module=model.owner_module,
        created_at=model.created_at,
        updated_at=model.updated_at,
        audit_ref=model.audit_ref,
        shot_id=model.shot_id,
        project_id=model.project_id,
        episode_id=model.episode_id,
        scene_id=model.scene_id,
        episode_ref=model.episode_ref,
        scene_ref=model.scene_ref,
        revision_no=model.revision_no,
        description=model.description,
        narrative_intent=model.narrative_intent,
        character_refs=model.character_refs,
        location_ref=model.location_ref,
        prop_refs=model.prop_refs,
        constraint_pack_applicability=model.constraint_pack_applicability,
        constraint_pack_id=model.constraint_pack_id,
        creative_autonomy=model.creative_autonomy,
        risk=model.risk,
        quality_profile=model.quality_profile,
        intent_ref=model.intent_ref,
        basis_script_revision_id=model.basis_script_revision_id,
        script_anchors=model.script_anchors,
        source_draft_ref=model.source_draft_ref,
        source_candidate_refs=model.source_candidate_refs,
        change_origin=model.change_origin,
        supersedes_shot_revision_id=model.supersedes_shot_revision_id,
        change_summary=model.change_summary,
        changed_fields=model.changed_fields,
        provenance=model.provenance,
        confirmed_by=model.confirmed_by,
        confirmed_at=model.confirmed_at,
        organization_id=model.organization_id,
    )


def _revision_to_orm(revision: ShotRevision) -> ShotRevisionModel:
    return ShotRevisionModel(
        entity_id=revision.entity_id,
        entity_type=revision.ENTITY_TYPE,
        schema_version=revision.schema_version,
        entity_version=revision.entity_version,
        status=revision.status.value,
        owner_module=revision.owner_module,
        tenant_id=revision.tenant_id,
        created_at=revision.created_at,
        created_by=revision.created_by,
        updated_at=revision.updated_at,
        audit_ref=revision.audit_ref,
        shot_revision_id=revision.shot_revision_id,
        shot_id=revision.shot_id,
        project_id=revision.project_id,
        episode_ref=revision.episode_ref,
        scene_ref=revision.scene_ref,
        episode_id=revision.episode_id,
        scene_id=revision.scene_id,
        description=revision.description,
        narrative_intent=revision.narrative_intent,
        character_refs=list(revision.character_refs),
        location_ref=revision.location_ref,
        prop_refs=list(revision.prop_refs),
        constraint_pack_applicability=revision.constraint_pack_applicability,
        constraint_pack_id=revision.constraint_pack_id,
        creative_autonomy=revision.creative_autonomy,
        risk=dict(revision.risk),
        quality_profile=revision.quality_profile,
        intent_ref=dict(revision.intent_ref),
        basis_script_revision_id=revision.basis_script_revision_id,
        script_anchors=list(revision.script_anchors),
        source_draft_ref=dict(revision.source_draft_ref)
        if revision.source_draft_ref is not None
        else None,
        source_candidate_refs=list(revision.source_candidate_refs),
        change_origin=revision.change_origin,
        supersedes_shot_revision_id=revision.supersedes_shot_revision_id,
        change_summary=revision.change_summary,
        changed_fields=list(revision.changed_fields),
        provenance=dict(revision.provenance),
        confirmed_by=revision.confirmed_by,
        confirmed_at=revision.confirmed_at,
        revision_no=revision.revision_no,
        organization_id=revision.organization_id,
    )


class SqlAlchemyShotRepository(ShotRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, shot: Shot) -> None:
        try:
            async with self._session.begin_nested():
                self._session.add(_shot_to_orm(shot))
        except (IntegrityError, ProgrammingError) as exc:
            translate_write_error(exc, entity_id=shot.entity_id)

    async def save(self, shot: Shot, expected_version: int) -> None:
        await conditional_update(
            self._session,
            ShotModel,
            shot.entity_id,
            expected_version,
            _orm_update_values(_shot_to_orm(shot)),
        )

    async def get_by_id(self, entity_id: str) -> Shot | None:
        result = await self._session.execute(
            select(ShotModel).where(ShotModel.entity_id == entity_id)
        )
        model = result.scalar_one_or_none()
        return _shot_from_orm(model) if model is not None else None

    async def get_by_project_code(
        self, project_id: str, shot_code: str
    ) -> Shot | None:
        result = await self._session.execute(
            select(ShotModel).where(
                ShotModel.project_id == project_id,
                ShotModel.shot_code == shot_code,
            )
        )
        model = result.scalar_one_or_none()
        return _shot_from_orm(model) if model is not None else None


class SqlAlchemyShotRevisionRepository(ShotRevisionRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, revision: ShotRevision) -> None:
        try:
            async with self._session.begin_nested():
                self._session.add(_revision_to_orm(revision))
        except (IntegrityError, ProgrammingError) as exc:
            translate_write_error(exc, entity_id=revision.entity_id)

    async def save(self, revision: ShotRevision, expected_version: int) -> None:
        await conditional_update(
            self._session,
            ShotRevisionModel,
            revision.entity_id,
            expected_version,
            _orm_update_values(_revision_to_orm(revision)),
        )

    async def get_by_id(self, entity_id: str) -> ShotRevision | None:
        result = await self._session.execute(
            select(ShotRevisionModel).where(ShotRevisionModel.entity_id == entity_id)
        )
        model = result.scalar_one_or_none()
        return _revision_from_orm(model) if model is not None else None

    async def active_head(self, shot_id: str) -> ShotRevision | None:
        result = await self._session.execute(
            select(ShotRevisionModel)
            .where(
                ShotRevisionModel.shot_id == shot_id,
                ShotRevisionModel.status == ShotRevisionStatus.ACTIVE.value,
            )
            .order_by(ShotRevisionModel.revision_no.desc())
            .limit(1)
        )
        model = result.scalar_one_or_none()
        return _revision_from_orm(model) if model is not None else None

    async def get_by_revision_no(
        self, shot_id: str, revision_no: int
    ) -> ShotRevision | None:
        result = await self._session.execute(
            select(ShotRevisionModel).where(
                ShotRevisionModel.shot_id == shot_id,
                ShotRevisionModel.revision_no == revision_no,
            )
        )
        model = result.scalar_one_or_none()
        return _revision_from_orm(model) if model is not None else None


class SqlReworkTaskVerifier(ReworkTaskVerifier):
    """只读校验返工授权单；不给镜头域 workflow 表写权限。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def exists_for_shot(
        self,
        task_id: str,
        *,
        project_id: str,
        tenant_id: UUID,
        organization_id: UUID | None,
    ) -> bool:
        sql = text(
            "SELECT 1 FROM workflow.rework_tasks "
            "WHERE rework_task_id = :task_id AND project_id = :project_id "
            "AND tenant_id = :tenant_id AND organization_id = :organization_id "
            "LIMIT 1"
        )
        result = await self._session.execute(
            sql,
            {
                "task_id": task_id,
                "project_id": project_id,
                "tenant_id": tenant_id,
                "organization_id": organization_id,
            },
        )
        return result.first() is not None
