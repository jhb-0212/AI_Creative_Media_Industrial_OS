"""ScriptRevision API 路由（薄层，只做协议适配与 DTO 转换）。

契约对齐：
- SCRIPT.IMPORT action：导入剧本版本，产出 DRAFT 态。
- 请求/响应走统一信封与错误信封。
- 业务逻辑下沉到 ScriptService，本层不写业务。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.shared.database import get_session
from services.project_planning.repository.script_sqlalchemy_repo import (
    SqlAlchemyScriptRepository,
)
from services.project_planning.service.script_service import ScriptService

router = APIRouter(prefix="/scripts", tags=["script-management"])


class ImportScriptRequest(BaseModel):
    """剧本导入请求体（SCRIPT.IMPORT）。"""

    project_id: str = Field(..., min_length=1, max_length=64)
    episode_ref: str = Field(..., min_length=1, max_length=64)
    title: str = Field(..., min_length=1, max_length=200)
    created_by: str = Field(..., min_length=1)
    entity_id: str | None = None


def _get_service(session: AsyncSession = Depends(get_session)) -> ScriptService:
    """服务装配：SQLAlchemy 仓储（独立 schema project_planning）。"""
    repository = SqlAlchemyScriptRepository(session)
    return ScriptService(repository)


@router.post("/import", status_code=201)
async def import_script(
    body: ImportScriptRequest,
    service: ScriptService = Depends(_get_service),
) -> dict:
    """导入剧本版本（幂等，产出 DRAFT 态）。"""
    try:
        script = await service.import_script(
            project_id=body.project_id,
            episode_ref=body.episode_ref,
            title=body.title,
            created_by=body.created_by,
            entity_id=body.entity_id,
        )
    except ValueError as exc:
        raise DomainError(
            ErrorRegistry.STATE_TRANSITION_DENIED,
            detail=str(exc),
        ) from exc
    return script.to_dict()


@router.get("/{entity_id}")
async def get_script(
    entity_id: str,
    service: ScriptService = Depends(_get_service),
) -> dict:
    """按 id 查询剧本版本。"""
    script = await service.get_script(entity_id)
    return script.to_dict()
