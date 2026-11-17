"""Shot API 路由（薄层，只做协议适配与 DTO 转换）。

契约对齐：
- SHOT.DRAFT.CREATE：AI 剧本拆镜产出草案（resource_type=ShotDraft）
- SHOT.REVISION.CONFIRM：授权人类确认草案产出正式修订（resource_type=ShotRevision）
- 请求/响应走统一信封与错误信封。
- 业务逻辑下沉到 ShotService，本层不写业务。
- 身份从可信 Principal 解析（created_by / tenant_id 均来自 JWT）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from packages.identity.authorization import ResourceRef
from packages.identity.enforcement import authorize
from packages.shared.contract_models import contract_model
from packages.shared.outbox_writer import SqlAlchemyOutboxWriter
from packages.shared.unit_of_work import UnitOfWork
from services.identity_access.deps import (
    Principal,
    get_read_shot_uow,
    get_shot_uow,
    require_action,
)
from services.shot_planning.repository.shot_draft_sqlalchemy_repo import (
    SqlAlchemyShotDraftRepository,
)
from services.shot_planning.repository.shot_revision_sqlalchemy_repo import (
    SqlAlchemyShotRevisionRepository,
)
from services.shot_planning.service.shot_service import ShotService

router = APIRouter(prefix="/shots", tags=["shot-planning"])

# 响应模型由契约定义生成（R3）：字段集恒等于 object_schemas 的对应定义。
_ShotDraftResponse = contract_model("ShotDraft")
_ShotRevisionResponse = contract_model("ShotRevision")


class CreateShotDraftRequest(BaseModel):
    """AI 拆镜草案请求体（身份不在请求体，由 Principal 提供）。"""

    project_id: str = Field(..., min_length=1, max_length=64)
    episode_ref: str = Field(..., min_length=1, max_length=64)
    scene_ref: str = Field(..., min_length=1, max_length=64)
    shot_code: str = Field(..., min_length=1, max_length=64)
    intent_summary: str = Field(..., min_length=1)
    shot_description: str = Field(..., min_length=1)
    entity_id: str | None = None


class ConfirmShotRevisionRequest(BaseModel):
    """人工确认草案产出正式修订的请求体。"""

    draft_id: str = Field(..., min_length=1, max_length=64)
    shot_id: str = Field(..., min_length=1, max_length=64)
    project_id: str = Field(..., min_length=1, max_length=64)
    entity_id: str | None = None


def _build_service(session: AsyncSession) -> ShotService:
    """服务装配：RLS 会话 + draft/revision 仓储端口 + Outbox 写入端口。"""
    return ShotService(
        SqlAlchemyShotDraftRepository(session),
        SqlAlchemyShotRevisionRepository(session),
        SqlAlchemyOutboxWriter(session),
    )


def _get_service(uow: UnitOfWork = Depends(get_shot_uow)) -> ShotService:
    """写用例服务装配（提交点由路由显式控制）。"""
    return _build_service(uow.session)


def _get_read_service(uow: UnitOfWork = Depends(get_read_shot_uow)) -> ShotService:
    """只读用例服务装配（只读事务，数据库层拒绝写入）。"""
    return _build_service(uow.session)


@router.post("/drafts", status_code=201, response_model=_ShotDraftResponse)
async def create_shot_draft(
    body: CreateShotDraftRequest,
    principal: Principal = Depends(require_action("SHOT.DRAFT.CREATE")),
    uow: UnitOfWork = Depends(get_shot_uow),
    service: ShotService = Depends(_get_service),
) -> dict[str, Any]:
    """AI 剧本拆镜：产出镜头草案。身份与归属键来自 Principal。"""
    authorize(
        principal,
        "SHOT.DRAFT.CREATE",
        resource=ResourceRef(resource_type="ShotDraft", project_id=body.project_id),
    )
    draft = await service.create_draft(
        project_id=body.project_id,
        episode_ref=body.episode_ref,
        scene_ref=body.scene_ref,
        shot_code=body.shot_code,
        intent_summary=body.intent_summary,
        shot_description=body.shot_description,
        principal=principal,
        entity_id=body.entity_id,
    )
    await uow.commit()
    return draft.to_dict()


@router.post("/revisions", status_code=201, response_model=_ShotRevisionResponse)
async def confirm_shot_revision(
    body: ConfirmShotRevisionRequest,
    principal: Principal = Depends(require_action("SHOT.REVISION.CONFIRM")),
    uow: UnitOfWork = Depends(get_shot_uow),
    service: ShotService = Depends(_get_service),
) -> dict[str, Any]:
    """授权人类确认草案，产出正式镜头修订（版本递增不覆盖）。"""
    authorize(
        principal,
        "SHOT.REVISION.CONFIRM",
        resource=ResourceRef(resource_type="ShotRevision", project_id=body.project_id),
    )
    revision = await service.confirm_revision(
        draft_id=body.draft_id,
        shot_id=body.shot_id,
        project_id=body.project_id,
        principal=principal,
        entity_id=body.entity_id,
    )
    await uow.commit()
    return revision.to_dict()
