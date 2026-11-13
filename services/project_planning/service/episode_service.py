"""Episode 应用服务（用例编排层）。

职责：
- 编排领域模型与仓储，实现「集数登记」用例。
- 归属验证：Episode 必须挂到真实存在的 Project（通过注入 ProjectRepository）。
- 幂等：同一 entity_id 重复登记返回既有集数。
- 抛出结构化 DomainError，不泄漏内部实现细节。

契约对齐：
- 8.1 核心实体：Episode 归属于 Project（Project → Episode 一对多）。
- 集数登记是剧本导入（SCRIPT.IMPORT）的前置——必须先有归属项目的
  Episode，才能挂载剧本版本。
"""

from __future__ import annotations

import uuid
from uuid import UUID

from sqlalchemy.exc import IntegrityError

from packages.errors.error_codes import DomainError, ErrorRegistry
from services.project_planning.domain.episode import Episode
from services.project_planning.repository.episode_repo import EpisodeRepository
from services.project_planning.repository.project_repo import ProjectRepository


class EpisodeService:
    """集数登记与查询用例。"""

    def __init__(
        self,
        repository: EpisodeRepository,
        project_repository: ProjectRepository,
    ) -> None:
        self._repository = repository
        self._project_repository = project_repository

    async def create_episode(
        self,
        *,
        project_id: str,
        episode_ref: str,
        title: str,
        created_by: str,
        tenant_id: UUID,
        entity_id: str | None = None,
    ) -> Episode:
        """登记集数（幂等，归属 Project）。

        契约要求：
        - episode 必须归属真实存在的 project（归属验证）。
        - 同项目内 episode_ref 唯一（数据库唯一约束兜底）。
        - 输入统一 strip。
        - tenant_id 来自可信身份上下文（Principal），是 RLS 隔离键。
        """
        project_id = project_id.strip()
        episode_ref = episode_ref.strip()
        title = title.strip()
        created_by = created_by.strip()

        if not project_id:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR, detail="project_id 不能为空"
            )
        if not episode_ref:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR, detail="episode_ref 不能为空"
            )
        if not title:
            raise DomainError(ErrorRegistry.VALIDATION_ERROR, detail="title 不能为空")
        if not created_by:
            raise DomainError(
                ErrorRegistry.VALIDATION_ERROR, detail="created_by 不能为空"
            )

        # 归属验证：project 必须存在。
        project = await self._project_repository.get_by_id(project_id)
        if project is None:
            raise DomainError(
                ErrorRegistry.NOT_FOUND,
                detail=f"项目不存在: {project_id}",
            )

        # 幂等：已存在同 id 直接返回。
        if entity_id is not None:
            existing = await self._repository.get_by_id(entity_id)
            if existing is not None:
                return existing

        episode = Episode(
            entity_id=entity_id or uuid.uuid4().hex,
            project_id=project_id,
            episode_ref=episode_ref,
            title=title,
            created_by=created_by,
            tenant_id=tenant_id,
        )

        # INSERT 语义新增；并发下唯一约束（project_id+episode_ref）兜底。
        try:
            await self._repository.add(episode)
        except IntegrityError:
            if entity_id is None:
                # 无显式 id 时，唯一约束冲突说明同项目已有同 episode_ref，
                # 降级为查询既有集数返回（幂等）。
                existing = await self._repository.get_by_project_ref(
                    project_id, episode_ref
                )
                if existing is not None:
                    return existing
            raise

        return episode

    async def get_episode(self, entity_id: str) -> Episode:
        """查询集数，不存在抛 NOT_FOUND。"""
        episode = await self._repository.get_by_id(entity_id)
        if episode is None:
            raise DomainError(ErrorRegistry.NOT_FOUND, detail=f"集数不存在: {entity_id}")
        return episode
