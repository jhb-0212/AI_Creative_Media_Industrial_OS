"""Project API 路由（薄层，只做协议适配与 DTO 转换）。

契约对齐：
- 请求/响应走统一信封与错误信封。
- 业务逻辑下沉到 ProjectService，本层不写业务。
- 身份从可信 Principal 解析（created_by / tenant_id 均来自 JWT，非客户端字符串）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from packages.shared.unit_of_work import UnitOfWork
from services.identity_access.deps import (
    Principal,
    get_read_tenant_uow,
    get_tenant_uow,
    require_role,
)
from services.project_planning.domain.enums import ProjectTier
from services.project_planning.repository.sqlalchemy_repo import SqlAlchemyProjectRepository
from services.project_planning.service.project_service import ProjectService

router = APIRouter(prefix="/projects", tags=["project-planning"])


class CreateProjectRequest(BaseModel):
    """创建项目请求体（身份不在请求体，由 Principal 提供）。"""

    project_name: str = Field(..., min_length=1, max_length=200)
    project_tier: ProjectTier = ProjectTier.B
    entity_id: str | None = None


def _build_service(session: AsyncSession) -> ProjectService:
    """服务装配：RLS 会话（已注入 tenant 上下文）+ SQLAlchemy 仓储。"""
    repository = SqlAlchemyProjectRepository(session)
    return ProjectService(repository)


def _get_service(uow: UnitOfWork = Depends(get_tenant_uow)) -> ProjectService:
    """写用例服务装配（提交点由路由显式控制）。"""
    return _build_service(uow.session)


def _get_read_service(
    uow: UnitOfWork = Depends(get_read_tenant_uow),
) -> ProjectService:
    """只读用例服务装配（只读事务，数据库层拒绝写入）。"""
    return _build_service(uow.session)


@router.post("", status_code=201)
async def create_project(
    body: CreateProjectRequest,
    principal: Principal = Depends(require_role("PROJECT.CREATE")),
    uow: UnitOfWork = Depends(get_tenant_uow),
    service: ProjectService = Depends(_get_service),
) -> dict[str, Any]:
    """创建项目（幂等）。created_by / tenant_id 来自可信 Principal。"""
    project = await service.create_project(
        project_name=body.project_name,
        project_tier=body.project_tier,
        created_by=principal.subject_id,
        tenant_id=principal.tenant_id,
        entity_id=body.entity_id,
    )
    # 显式提交：在返回响应之前落库，不依赖依赖退出码（响应发出后才执行）。
    await uow.commit()
    return project.to_dict()


@router.get("/{entity_id}")
async def get_project(
    entity_id: str,
    _principal: Principal = Depends(require_role("PROJECT.READ")),
    service: ProjectService = Depends(_get_read_service),
) -> dict[str, Any]:
    """按 id 查询项目（需 PROJECT.READ 授权；租户边界由 RLS 保证）。"""
    project = await service.get_project(entity_id)
    return project.to_dict()
