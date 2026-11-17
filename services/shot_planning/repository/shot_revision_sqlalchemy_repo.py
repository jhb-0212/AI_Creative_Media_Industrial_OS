"""ShotRevision SQLAlchemy 仓储实现（数据所有权：shot_planning 模块专属写入口）。

修订是 append-only 事实：新修订即新行，旧修订永远保留，绝不原地改写；
最新版本由 revision 号派生（get_active_by_shot 取 revision 最大者）。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.shared.persistence import translate_integrity_errors
from services.shot_planning.domain.enums import ShotRevisionStatus
from services.shot_planning.domain.shot_revision import ShotRevision
from services.shot_planning.repository.orm import ShotRevisionModel
from services.shot_planning.repository.shot_revision_repo import ShotRevisionRepository


def _to_domain(model: ShotRevisionModel) -> ShotRevision:
    """ORM -> 领域模型（枚举反序列化防御：非法值降级为结构化 DomainError）。"""
    try:
        status = ShotRevisionStatus(model.status)
    except ValueError as exc:
        raise DomainError(
            ErrorRegistry.INTERNAL_ERROR,
            detail=f"shot_revisions 存量状态非法: {model.status!r}",
        ) from exc
    return ShotRevision(
        entity_id=model.entity_id,
        shot_id=model.shot_id,
        project_id=model.project_id,
        episode_ref=model.episode_ref,
        scene_ref=model.scene_ref,
        intent_summary=model.intent_summary,
        shot_description=model.shot_description,
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


def _to_orm(revision: ShotRevision) -> ShotRevisionModel:
    """领域模型 -> ORM。"""
    return ShotRevisionModel(
        entity_id=revision.entity_id,
        entity_type=revision.ENTITY_TYPE,
        schema_version=revision.schema_version,
        entity_version=revision.entity_version,
        status=revision.status.value,
        owner_module=revision.owner_module,
        tenant_id=revision.tenant_id,
        organization_id=revision.organization_id,
        created_at=revision.created_at,
        created_by=revision.created_by,
        updated_at=revision.updated_at,
        audit_ref=revision.audit_ref,
        shot_id=revision.shot_id,
        project_id=revision.project_id,
        episode_ref=revision.episode_ref,
        scene_ref=revision.scene_ref,
        intent_summary=revision.intent_summary,
        shot_description=revision.shot_description,
        revision=revision.revision,
    )


class SqlAlchemyShotRevisionRepository(ShotRevisionRepository):
    """基于 SQLAlchemy 异步会话的仓储实现。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, revision: ShotRevision) -> None:
        # INSERT 语义 + SAVEPOINT：同 shot 内 revision 唯一约束兜底并发。
        with translate_integrity_errors(entity=f"shot_revision_id={revision.entity_id}"):
            async with self._session.begin_nested():
                self._session.add(_to_orm(revision))

    async def get_by_id(self, entity_id: str) -> ShotRevision | None:
        stmt = select(ShotRevisionModel).where(
            ShotRevisionModel.entity_id == entity_id
        )
        result = await self._session.execute(stmt)
        model = result.scalar_one_or_none()
        return _to_domain(model) if model is not None else None

    async def get_active_by_shot(self, shot_id: str) -> ShotRevision | None:
        """按 shot_id 查询最新修订（revision 最大者）。"""
        stmt = (
            select(ShotRevisionModel)
            .where(ShotRevisionModel.shot_id == shot_id)
            .order_by(ShotRevisionModel.revision.desc())
            .limit(1)
        )
        result = await self._session.execute(stmt)
        model = result.scalar_one_or_none()
        return _to_domain(model) if model is not None else None
