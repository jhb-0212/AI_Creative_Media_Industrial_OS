"""Episode SQLAlchemy 仓储实现（数据所有权：project_planning 模块专属写入口）。

设计要点：
- 实现 EpisodeRepository 抽象接口，与内存实现可互换。
- 领域模型 <-> ORM 模型的双向映射集中在此，隔离持久化细节。
- 只经本仓储写 project_planning.episodes 表，其它模块禁止直写。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.errors.error_codes import DomainError, ErrorRegistry
from services.project_planning.domain.enums import EpisodeStatus
from services.project_planning.domain.episode import Episode
from services.project_planning.repository.episode_repo import EpisodeRepository
from services.project_planning.repository.orm import EpisodeModel


def _to_domain(model: EpisodeModel) -> Episode:
    """ORM -> 领域模型。

    枚举反序列化做防御：存量数据中若出现契约外的非法状态值，
    降级为结构化 DomainError（数据一致性），而非裸 ValueError 导致 500。
    """
    try:
        status = EpisodeStatus(model.status)
    except ValueError as exc:
        raise DomainError(
            ErrorRegistry.INTERNAL_ERROR,
            detail=f"episodes 存量状态非法: {model.status!r}",
        ) from exc
    return Episode(
        entity_id=model.entity_id,
        project_id=model.project_id,
        episode_ref=model.episode_ref,
        title=model.title,
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


def _to_orm(episode: Episode) -> EpisodeModel:
    """领域模型 -> ORM。"""
    return EpisodeModel(
        entity_id=episode.entity_id,
        entity_type=episode.entity_type,
        schema_version=episode.schema_version,
        entity_version=episode.entity_version,
        status=episode.status.value,
        owner_module=episode.owner_module,
        created_at=episode.created_at,
        created_by=episode.created_by,
        updated_at=episode.updated_at,
        audit_ref=episode.audit_ref,
        project_id=episode.project_id,
        episode_ref=episode.episode_ref,
        title=episode.title,
    )


class SqlAlchemyEpisodeRepository(EpisodeRepository):
    """基于 SQLAlchemy 异步会话的仓储实现。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, episode: Episode) -> None:
        # INSERT 语义 + SAVEPOINT：并发重复插入由唯一约束捕获（幂等兜底）。
        async with self._session.begin_nested():
            self._session.add(_to_orm(episode))

    async def save(self, episode: Episode) -> None:
        # 仅登记变更，不 commit：事务边界由请求层统一控制。
        await self._session.merge(_to_orm(episode))

    async def get_by_id(self, entity_id: str) -> Episode | None:
        stmt = select(EpisodeModel).where(EpisodeModel.entity_id == entity_id)
        result = await self._session.execute(stmt)
        model = result.scalar_one_or_none()
        return _to_domain(model) if model is not None else None

    async def get_by_project_ref(
        self, project_id: str, episode_ref: str
    ) -> Episode | None:
        """按项目 + 集数标识查询集数（归属验证核心查询）。"""
        stmt = select(EpisodeModel).where(
            EpisodeModel.project_id == project_id,
            EpisodeModel.episode_ref == episode_ref,
        )
        result = await self._session.execute(stmt)
        model = result.scalar_one_or_none()
        return _to_domain(model) if model is not None else None
