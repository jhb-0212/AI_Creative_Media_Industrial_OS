"""Project SQLAlchemy 仓储实现（数据所有权：project_planning 模块专属写入口）。

设计要点：
- 实现 ProjectRepository 抽象接口，与内存实现可互换。
- 领域模型 <-> ORM 模型的双向映射集中在此，隔离持久化细节。
- 只经本仓储写 project_planning.projects 表，其它模块禁止直写。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.errors.error_codes import DomainError, ErrorRegistry
from services.project_planning.domain.enums import ProjectStatus, ProjectTier
from services.project_planning.domain.project import Project
from services.project_planning.repository.orm import ProjectModel
from services.project_planning.repository.project_repo import ProjectRepository


def _to_domain(model: ProjectModel) -> Project:
    """ORM -> 领域模型。

    枚举反序列化做防御：存量数据若出现契约外非法值，降级为结构化
    DomainError（数据一致性），而非裸 ValueError 导致 500。
    """
    try:
        tier = ProjectTier(model.project_tier)
        status = ProjectStatus(model.status)
    except ValueError as exc:
        raise DomainError(
            ErrorRegistry.INTERNAL_ERROR,
            detail=(
                f"projects 存量枚举非法: tier={model.project_tier!r}, "
                f"status={model.status!r}"
            ),
        ) from exc
    return Project(
        entity_id=model.entity_id,
        project_name=model.project_name,
        project_tier=tier,
        created_by=model.created_by,
        status=status,
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

    async def add(self, project: Project) -> None:
        # INSERT 语义 + SAVEPOINT：并发重复插入相同主键时由数据库唯一约束
        # 抛出 IntegrityError；用嵌套事务（savepoint）隔离，失败仅回滚本次
        # INSERT，使 session 不进入 aborted 态，服务层可安全降级为幂等查询。
        async with self._session.begin_nested():
            self._session.add(_to_orm(project))

    async def save(self, project: Project) -> None:
        # 仅登记变更，不 commit：事务边界由请求层（get_session 依赖）统一控制，
        # 保证多步用例中途失败可整体回滚。
        await self._session.merge(_to_orm(project))

    async def get_by_id(self, entity_id: str) -> Project | None:
        stmt = select(ProjectModel).where(ProjectModel.entity_id == entity_id)
        result = await self._session.execute(stmt)
        model = result.scalar_one_or_none()
        return _to_domain(model) if model is not None else None
