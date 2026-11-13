"""Project SQLAlchemy 仓储实现（数据所有权：project_planning 模块专属写入口）。

设计要点：
- 实现 ProjectRepository 抽象接口，与内存实现可互换。
- 领域模型 <-> ORM 模型的双向映射集中在此，隔离持久化细节。
- 只经本仓储写 project_planning.projects 表，其它模块禁止直写。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from services.project_planning.domain.enums import ProjectStatus, ProjectTier
from services.project_planning.domain.project import Project
from services.project_planning.repository.orm import ProjectModel
from services.project_planning.repository.project_repo import ProjectRepository


def _to_domain(model: ProjectModel) -> Project:
    """ORM -> 领域模型。"""
    return Project(
        entity_id=model.entity_id,
        project_name=model.project_name,
        project_tier=ProjectTier(model.project_tier),
        created_by=model.created_by,
        status=ProjectStatus(model.status),
        entity_type=model.entity_type,
        schema_version=model.schema_version,
        entity_version=model.entity_version,
        owner_module=model.owner_module,
        created_at=model.created_at,
        updated_at=model.updated_at,
        audit_ref=model.audit_ref,
    )


def _to_orm(project: Project) -> ProjectModel:
    """领域模型 -> ORM。"""
    return ProjectModel(
        entity_id=project.entity_id,
        entity_type=project.entity_type,
        schema_version=project.schema_version,
        entity_version=project.entity_version,
        status=project.status.value,
        owner_module=project.owner_module,
        created_at=project.created_at,
        created_by=project.created_by,
        updated_at=project.updated_at,
        audit_ref=project.audit_ref,
        project_name=project.project_name,
        project_tier=project.project_tier.value,
    )


class SqlAlchemyProjectRepository(ProjectRepository):
    """基于 SQLAlchemy 异步会话的仓储实现。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save(self, project: Project) -> None:
        await self._session.merge(_to_orm(project))
        await self._session.commit()

    async def get_by_id(self, entity_id: str) -> Project | None:
        stmt = select(ProjectModel).where(ProjectModel.entity_id == entity_id)
        result = await self._session.execute(stmt)
        model = result.scalar_one_or_none()
        return _to_domain(model) if model is not None else None
