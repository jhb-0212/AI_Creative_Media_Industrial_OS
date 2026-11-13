"""ScriptRevision SQLAlchemy 仓储实现（数据所有权：project_planning 模块专属写入口）。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from services.project_planning.domain.enums import ScriptStatus
from services.project_planning.domain.script import ScriptRevision
from services.project_planning.repository.orm import ScriptModel
from services.project_planning.repository.script_repo import ScriptRepository


def _to_domain(model: ScriptModel) -> ScriptRevision:
    """ORM -> 领域模型。"""
    return ScriptRevision(
        entity_id=model.entity_id,
        project_id=model.project_id,
        episode_ref=model.episode_ref,
        title=model.title,
        created_by=model.created_by,
        status=ScriptStatus(model.status),
        entity_type=model.entity_type,
        schema_version=model.schema_version,
        entity_version=model.entity_version,
        owner_module=model.owner_module,
        created_at=model.created_at,
        updated_at=model.updated_at,
        audit_ref=model.audit_ref,
    )


def _to_orm(script: ScriptRevision) -> ScriptModel:
    """领域模型 -> ORM。"""
    return ScriptModel(
        entity_id=script.entity_id,
        entity_type=script.entity_type,
        schema_version=script.schema_version,
        entity_version=script.entity_version,
        status=script.status.value,
        owner_module=script.owner_module,
        created_at=script.created_at,
        created_by=script.created_by,
        updated_at=script.updated_at,
        audit_ref=script.audit_ref,
        project_id=script.project_id,
        episode_ref=script.episode_ref,
        title=script.title,
    )


class SqlAlchemyScriptRepository(ScriptRepository):
    """基于 SQLAlchemy 异步会话的仓储实现。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save(self, script: ScriptRevision) -> None:
        await self._session.merge(_to_orm(script))
        await self._session.commit()

    async def get_by_id(self, entity_id: str) -> ScriptRevision | None:
        stmt = select(ScriptModel).where(ScriptModel.entity_id == entity_id)
        result = await self._session.execute(stmt)
        model = result.scalar_one_or_none()
        return _to_domain(model) if model is not None else None

    async def get_latest_by_project(self, project_id: str) -> ScriptRevision | None:
        """查询项目最新的未取代版本（DRAFT/LOCKED 态）。"""
        stmt = (
            select(ScriptModel)
            .where(
                ScriptModel.project_id == project_id,
                ScriptModel.status != ScriptStatus.SUPERSEDED.value,
            )
            .order_by(ScriptModel.created_at.desc())
            .limit(1)
        )
        result = await self._session.execute(stmt)
        model = result.scalar_one_or_none()
        return _to_domain(model) if model is not None else None
