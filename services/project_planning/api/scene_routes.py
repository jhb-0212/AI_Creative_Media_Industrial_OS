"""Scene API 路由（薄层，只做协议适配与 DTO 转换）。

契约对齐（V205-R14-A13）：
- 8.1 核心实体：Scene 归属于 Episode，作用域收敛到 Project。
- 场次登记是镜头域的第一环：没有场次，``shots.scene_id NOT NULL`` 无合法值。
- 请求/响应走统一信封与错误信封。
- 业务逻辑下沉到 SceneService，本层不写业务。
- 身份从可信 Principal 解析（created_by / tenant_id 均来自 JWT）。

授权口径（与 EPISODE.CREATE 同形，理由同）：
- 创建时被操作资源尚不存在，判定对象是**父项目**——否则可以先在别人的项目里
  造场次，再借后续读路径访问（写入即越权）。集归属由服务层与复合外键共同保证。
- 读与状态推进判定对象是场次自身（``require_resource_role``），归属经
  ``scenes → projects`` 解析。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from packages.shared.contract_models import contract_model
from packages.shared.unit_of_work import UnitOfWork
from services.identity_access.deps import (
    Principal,
    authorize_on_parent,
    get_project_tenant_uow,
    get_read_project_tenant_uow,
    require_resource_role,
    require_role,
)
from services.project_planning.domain.enums import SceneStatus
from services.project_planning.repository.episode_sqlalchemy_repo import (
    SqlAlchemyEpisodeRepository,
)
from services.project_planning.repository.scene_sqlalchemy_repo import (
    SqlAlchemySceneRepository,
)
from services.project_planning.repository.sqlalchemy_outbox_writer import (
    SqlAlchemyOutboxWriter,
)
from services.project_planning.repository.sqlalchemy_repo import (
    SqlAlchemyProjectRepository,
)
from services.project_planning.service.scene_service import SceneService

router = APIRouter(prefix="/scenes", tags=["scene-management"])

# 响应模型由契约定义生成（R3）：字段集恒等于 object_schemas 的 Scene 定义。
_SceneResponse = contract_model("Scene")


class CreateSceneRequest(BaseModel):
    """场次登记请求体（身份不在请求体，由 Principal 提供）。

    用 ``episode_ref`` 而不是 ``episode_id`` 定位父集：与本域既有的寻址口径
    （``EPISODE.CREATE`` / ``SCRIPT.IMPORT``）一致。真实的 ``episode_id`` 由
    服务层解析后写入响应。
    """

    project_id: str = Field(..., min_length=1, max_length=64)
    episode_ref: str = Field(..., min_length=1, max_length=64)
    scene_code: str = Field(..., min_length=1, max_length=64)
    entity_id: str | None = None


class ChangeSceneStatusRequest(BaseModel):
    """场次状态变更请求体（SCENE.STATUS.CHANGE）。

    目标状态作参数；转移合法性由领域状态机在运行时校验。
    """

    target_status: SceneStatus = Field(..., description="目标状态")


def _build_service(session: AsyncSession) -> SceneService:
    """服务装配：RLS 会话 + scene/episode/project 仓储端口 + Outbox 写入端口。"""
    return SceneService(
        SqlAlchemySceneRepository(session),
        SqlAlchemyEpisodeRepository(session),
        SqlAlchemyProjectRepository(session),
        SqlAlchemyOutboxWriter(session),
    )


def _get_service(uow: UnitOfWork = Depends(get_project_tenant_uow)) -> SceneService:
    """写用例服务装配（提交点由路由显式控制）。"""
    return _build_service(uow.session)


def _get_read_service(
    uow: UnitOfWork = Depends(get_read_project_tenant_uow),
) -> SceneService:
    """只读用例服务装配（只读事务，数据库层拒绝写入）。"""
    return _build_service(uow.session)


@router.post("", status_code=201, response_model=_SceneResponse)
async def create_scene(
    body: CreateSceneRequest,
    principal: Principal = Depends(require_role("SCENE.CREATE")),
    uow: UnitOfWork = Depends(get_project_tenant_uow),
    service: SceneService = Depends(_get_service),
) -> dict[str, Any]:
    """登记场次（幂等，归属 Episode 与 Project）。created_by / tenant_id 来自 Principal。"""
    await authorize_on_parent(
        "SCENE.CREATE",
        principal,
        uow.session,
        parent_type="Project",
        parent_id=body.project_id,
    )
    scene = await service.create_scene(
        project_id=body.project_id,
        episode_ref=body.episode_ref,
        scene_code=body.scene_code,
        created_by=principal.subject_id,
        tenant_id=principal.tenant_id,
        entity_id=body.entity_id,
    )
    await uow.commit()
    return scene.to_dict()


@router.get("/{entity_id}", response_model=_SceneResponse)
async def get_scene(
    entity_id: str,
    _principal: Principal = Depends(
        require_resource_role(
            "SCENE.READ",
            resource_type="Scene",
            param_name="entity_id",
            uow_dep=get_read_project_tenant_uow,
        )
    ),
    service: SceneService = Depends(_get_read_service),
) -> dict[str, Any]:
    """按 id 查询场次（租户边界由 RLS 保证，组织边界由统一授权入口判定）。"""
    scene = await service.get_scene(entity_id)
    return scene.to_dict()


@router.post("/{entity_id}/status", response_model=_SceneResponse)
async def change_scene_status(
    entity_id: str,
    body: ChangeSceneStatusRequest,
    principal: Principal = Depends(
        require_resource_role(
            "SCENE.STATUS.CHANGE",
            resource_type="Scene",
            param_name="entity_id",
            uow_dep=get_project_tenant_uow,
        )
    ),
    uow: UnitOfWork = Depends(get_project_tenant_uow),
    service: SceneService = Depends(_get_service),
) -> dict[str, Any]:
    """推进场次状态（SCENE.STATUS.CHANGE）。

    changed_by = 当前 Principal；目标状态合法性由领域状态机校验，终态不可复活。
    归档时的「不得有在制镜头」由数据库守卫拒绝，本层不复述该规则。
    """
    scene = await service.change_status(
        entity_id=entity_id,
        target_status=body.target_status,
        changed_by=principal.subject_id,
        tenant_id=principal.tenant_id,
    )
    await uow.commit()
    return scene.to_dict()
