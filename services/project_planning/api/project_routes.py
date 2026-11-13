"""Project API 路由（薄层，只做协议适配与 DTO 转换）。

契约对齐：
- 请求/响应走统一信封与错误信封。
- 业务逻辑下沉到 ProjectService，本层不写业务。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from packages.errors.error_codes import DomainError, ErrorRegistry
from services.project_planning.domain.enums import ProjectTier
from services.project_planning.repository.in_memory_repo import InMemoryProjectRepository
from services.project_planning.service.project_service import ProjectService

router = APIRouter(prefix="/projects", tags=["project-planning"])


class CreateProjectRequest(BaseModel):
    """创建项目请求体。"""

    project_name: str = Field(..., min_length=1, max_length=200)
    project_tier: ProjectTier = ProjectTier.B
    created_by: str = Field(..., min_length=1)
    entity_id: str | None = None


def _get_service() -> ProjectService:
    """服务装配（Phase 1 使用内存仓储，后续替换为 PostgreSQL 实现）。"""
    repository = InMemoryProjectRepository()
    return ProjectService(repository)


@router.post("", status_code=201)
async def create_project(
    body: CreateProjectRequest,
    service: ProjectService = Depends(_get_service),
) -> dict:
    """创建项目（幂等）。"""
    try:
        project = await service.create_project(
            project_name=body.project_name,
            project_tier=body.project_tier,
            created_by=body.created_by,
            entity_id=body.entity_id,
        )
    except ValueError as exc:
        raise DomainError(
            ErrorRegistry.STATE_TRANSITION_DENIED,
            detail=str(exc),
        ) from exc
    return project.to_dict()


@router.get("/{entity_id}")
async def get_project(
    entity_id: str,
    service: ProjectService = Depends(_get_service),
) -> dict:
    """按 id 查询项目。"""
    project = await service.get_project(entity_id)
    return project.to_dict()
