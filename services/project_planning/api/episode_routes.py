"""Episode API 路由（薄层，只做协议适配与 DTO 转换）。

契约对齐：
- 8.1 核心实体：Episode 归属于 Project。
- 集数登记是剧本导入（SCRIPT.IMPORT）的前置。
- 请求/响应走统一信封与错误信封。
- 业务逻辑下沉到 EpisodeService，本层不写业务。
- 身份从可信 Principal 解析（created_by / tenant_id 均来自 JWT）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from packages.errors.error_codes import DomainError, ErrorRegistry
from services.identity_access.deps import Principal, get_principal, get_tenant_session
from services.project_planning.repository.episode_sqlalchemy_repo import (
    SqlAlchemyEpisodeRepository,
)
from services.project_planning.repository.sqlalchemy_repo import (
    SqlAlchemyProjectRepository,
)
from services.project_planning.service.episode_service import EpisodeService

router = APIRouter(prefix="/episodes", tags=["episode-management"])


class CreateEpisodeRequest(BaseModel):
    """集数登记请求体（身份不在请求体，由 Principal 提供）。"""

    project_id: str = Field(..., min_length=1, max_length=64)
    episode_ref: str = Field(..., min_length=1, max_length=64)
    title: str = Field(..., min_length=1, max_length=200)
    entity_id: str | None = None


def _get_service(
    session: AsyncSession = Depends(get_tenant_session),
) -> EpisodeService:
    """服务装配：RLS 会话 + episode/project 仓储端口注入。"""
    episode_repository = SqlAlchemyEpisodeRepository(session)
    project_repository = SqlAlchemyProjectRepository(session)
    return EpisodeService(episode_repository, project_repository)


@router.post("", status_code=201)
async def create_episode(
    body: CreateEpisodeRequest,
    principal: Principal = Depends(get_principal),
    service: EpisodeService = Depends(_get_service),
) -> dict[str, Any]:
    """登记集数（幂等，归属 Project）。created_by / tenant_id 来自 Principal。"""
    try:
        episode = await service.create_episode(
            project_id=body.project_id,
            episode_ref=body.episode_ref,
            title=body.title,
            created_by=principal.subject_id,
            tenant_id=principal.tenant_id,
            entity_id=body.entity_id,
        )
    except ValueError as exc:
        raise DomainError(
            ErrorRegistry.STATE_TRANSITION_DENIED,
            detail=str(exc),
        ) from exc
    return episode.to_dict()


@router.get("/{entity_id}")
async def get_episode(
    entity_id: str,
    service: EpisodeService = Depends(_get_service),
) -> dict[str, Any]:
    """按 id 查询集数。"""
    episode = await service.get_episode(entity_id)
    return episode.to_dict()
