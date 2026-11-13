"""ScriptRevision API 路由（薄层，只做协议适配与 DTO 转换）。

契约对齐：
- SCRIPT.IMPORT action：导入剧本版本，产出 DRAFT 态。
- SCRIPT.LOCK.REQUEST：申请人提交锁定申请（DRAFT→LOCK_PENDING）。
- SCRIPT.LOCK.APPROVE：批准人批准（LOCK_PENDING→LOCKED），职责分离。
- 请求/响应走统一信封与错误信封。
- 业务逻辑下沉到 ScriptService，本层不写业务。
- 身份从可信 Principal 解析（created_by / requested_by / approved_by 均来自 JWT）。

两步审批流（身份治理单元）：
- 申请：requested_by = 当前 Principal（调用者本人）。
- 批准：approved_by = 当前 Principal（另一个主体），服务层做职责分离比对。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from packages.errors.error_codes import DomainError, ErrorRegistry
from services.identity_access.deps import Principal, get_tenant_session, require_role
from services.project_planning.repository.episode_sqlalchemy_repo import (
    SqlAlchemyEpisodeRepository,
)
from services.project_planning.repository.script_sqlalchemy_repo import (
    SqlAlchemyScriptRepository,
)
from services.project_planning.repository.sqlalchemy_outbox_writer import (
    SqlAlchemyOutboxWriter,
)
from services.project_planning.repository.sqlalchemy_repo import (
    SqlAlchemyProjectRepository,
)
from services.project_planning.service.script_service import ScriptService

router = APIRouter(prefix="/scripts", tags=["script-management"])


class ImportScriptRequest(BaseModel):
    """剧本导入请求体（SCRIPT.IMPORT）。身份由 Principal 提供。"""

    project_id: str = Field(..., min_length=1, max_length=64)
    episode_ref: str = Field(..., min_length=1, max_length=64)
    title: str = Field(..., min_length=1, max_length=200)
    entity_id: str | None = None


def _get_service(
    session: AsyncSession = Depends(get_tenant_session),
) -> ScriptService:
    """服务装配：RLS 会话 + 三个仓储端口 + Outbox 写入端口注入。

    归属验证需要 ProjectRepository（验证项目存在）与 EpisodeRepository
    （验证集数归属）；审计事件经 OutboxWriter 端口与状态变更同事务写入。
    """
    script_repository = SqlAlchemyScriptRepository(session)
    project_repository = SqlAlchemyProjectRepository(session)
    episode_repository = SqlAlchemyEpisodeRepository(session)
    outbox_writer = SqlAlchemyOutboxWriter(session)
    return ScriptService(
        script_repository,
        project_repository,
        episode_repository,
        outbox_writer,
    )


@router.post("/import", status_code=201)
async def import_script(
    body: ImportScriptRequest,
    principal: Principal = Depends(require_role("SCRIPT.IMPORT")),
    service: ScriptService = Depends(_get_service),
) -> dict[str, Any]:
    """导入剧本版本（幂等，产出 DRAFT 态）。created_by / tenant_id 来自 Principal。"""
    try:
        script = await service.import_script(
            project_id=body.project_id,
            episode_ref=body.episode_ref,
            title=body.title,
            principal=principal,
            entity_id=body.entity_id,
        )
    except ValueError as exc:
        raise DomainError(
            ErrorRegistry.STATE_TRANSITION_DENIED,
            detail=str(exc),
        ) from exc
    return script.to_dict()


@router.post("/{entity_id}/lock/request")
async def request_lock(
    entity_id: str,
    principal: Principal = Depends(require_role("SCRIPT.LOCK.REQUEST")),
    service: ScriptService = Depends(_get_service),
) -> dict[str, Any]:
    """提交锁定申请（SCRIPT.LOCK.REQUEST，DRAFT→LOCK_PENDING）。

    requested_by = 当前 Principal（调用者本人），非客户端字符串。
    """
    try:
        script = await service.request_lock(entity_id=entity_id, principal=principal)
    except ValueError as exc:
        raise DomainError(
            ErrorRegistry.STATE_TRANSITION_DENIED,
            detail=str(exc),
        ) from exc
    return script.to_dict()


@router.post("/{entity_id}/lock/approve")
async def approve_lock(
    entity_id: str,
    principal: Principal = Depends(require_role("SCRIPT.LOCK.APPROVE")),
    service: ScriptService = Depends(_get_service),
) -> dict[str, Any]:
    """批准锁定（SCRIPT.LOCK.APPROVE，LOCK_PENDING→LOCKED）。

    approved_by = 当前 Principal，服务层做职责分离比对（批准人 != 申请人）。
    """
    try:
        script = await service.approve_lock(entity_id=entity_id, principal=principal)
    except ValueError as exc:
        raise DomainError(
            ErrorRegistry.STATE_TRANSITION_DENIED,
            detail=str(exc),
        ) from exc
    return script.to_dict()


@router.get("/{entity_id}")
async def get_script(
    entity_id: str,
    _principal: Principal = Depends(require_role("SCRIPT.READ")),
    service: ScriptService = Depends(_get_service),
) -> dict[str, Any]:
    """按 id 查询剧本版本（需 SCRIPT.READ 授权；租户边界由 RLS 保证）。"""
    script = await service.get_script(entity_id)
    return script.to_dict()
