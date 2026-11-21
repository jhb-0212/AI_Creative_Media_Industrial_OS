"""Shot API 路由（薄层，只做协议适配、授权装配与 DTO 转换）。

业务规则不在这里：版本冲突、父链冻结、守卫字段与终态不可复活全部下沉到
ShotService / 领域状态机 / 数据库约束。路由只保证两件事：
1. 动作级授权一定挂在端点上（``require_role`` / ``require_resource_role``）；
2. 资源级归属判定走统一入口——新建镜头判 Scene，后续修订判已有 Shot。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from packages.shared.contract_models import contract_model
from packages.shared.unit_of_work import UnitOfWork
from services.identity_access.deps import (
    Principal,
    authorize_on_parent,
    authorize_on_resource,
    get_project_tenant_uow,
    require_resource_role,
    require_role,
)
from services.project_planning.domain.enums import ShotStatus
from services.project_planning.repository.episode_sqlalchemy_repo import (
    SqlAlchemyEpisodeRepository,
)
from services.project_planning.repository.scene_sqlalchemy_repo import (
    SqlAlchemySceneRepository,
)
from services.project_planning.repository.shot_sqlalchemy_repo import (
    SqlAlchemyShotRepository,
    SqlAlchemyShotRevisionRepository,
    SqlReworkTaskVerifier,
)
from services.project_planning.repository.sqlalchemy_outbox_writer import (
    SqlAlchemyOutboxWriter,
)
from services.project_planning.repository.sqlalchemy_repo import (
    SqlAlchemyProjectRepository,
)
from services.project_planning.service.shot_service import (
    ConfirmRevisionCommand,
    ShotService,
)

router = APIRouter(prefix="/shots", tags=["shot-management"])

_ShotResponse = contract_model("Shot")
_ShotRevisionResponse = contract_model("ShotRevision")


class ConfirmShotRevisionRequest(BaseModel):
    """SHOT.REVISION.CONFIRM 载荷（身份来自 Principal，不接受 confirmed_by 覆盖）。"""

    project_id: str = Field(..., min_length=1, max_length=64)
    scene_id: str = Field(..., min_length=1, max_length=64)
    shot_code: str = Field(..., min_length=1, max_length=64)
    description: str = Field(..., min_length=1)
    narrative_intent: str = Field(..., min_length=1)
    location_ref: str = Field(..., min_length=1, max_length=256)
    basis_script_revision_id: str = Field(..., min_length=1, max_length=64)
    script_anchors: list[str] = Field(..., min_length=1)
    character_refs: list[str] = Field(default_factory=list)
    prop_refs: list[str] = Field(default_factory=list)
    constraint_pack_applicability: str = "NOT_APPLICABLE"
    constraint_pack_id: str | None = None
    creative_autonomy: str = "L1"
    risk: dict[str, Any] | None = None
    quality_profile: str = "UNIFIED_HIGH_QUALITY"
    intent_ref: dict[str, Any] | None = None
    source_draft_ref: dict[str, Any] | None = None
    source_candidate_refs: list[str] = Field(default_factory=list)
    change_origin: str = "BREAKDOWN_CONFIRM"
    change_summary: str | None = None
    changed_fields: list[str] = Field(default_factory=list)
    provenance: dict[str, Any] | None = None
    # 后续修订必填：同 shot_id + 当前 ACTIVE 修订 id，服务端据此做版本冲突拒绝。
    shot_id: str | None = None
    expected_shot_revision_id: str | None = None


class ChangeShotStatusRequest(BaseModel):
    target_status: ShotStatus
    blocked_reason: str | None = None
    rework_basis_ref: str | None = None
    cancellation_ref: str | None = None
    cancelled_at: datetime | None = None


def _build_service(session: AsyncSession) -> ShotService:
    return ShotService(
        shots=SqlAlchemyShotRepository(session),
        revisions=SqlAlchemyShotRevisionRepository(session),
        scenes=SqlAlchemySceneRepository(session),
        episodes=SqlAlchemyEpisodeRepository(session),
        projects=SqlAlchemyProjectRepository(session),
        rework_tasks=SqlReworkTaskVerifier(session),
        outbox_writer=SqlAlchemyOutboxWriter(session),
    )


def _get_service(uow: UnitOfWork = Depends(get_project_tenant_uow)) -> ShotService:
    return _build_service(uow.session)


@router.post("/revisions/confirm")
async def confirm_shot_revision(
    body: ConfirmShotRevisionRequest,
    principal: Principal = Depends(require_role("SHOT.REVISION.CONFIRM")),
    uow: UnitOfWork = Depends(get_project_tenant_uow),
    service: ShotService = Depends(_get_service),
) -> dict[str, Any]:
    """确认镜头内容；首次原子创建 Shot + 首版修订，后续只新增修订并前移指针。"""
    if body.shot_id is not None:
        await authorize_on_resource(
            "SHOT.REVISION.CONFIRM",
            principal,
            uow.session,
            resource_type="Shot",
            resource_id=body.shot_id,
        )
    else:
        await authorize_on_parent(
            "SHOT.REVISION.CONFIRM",
            principal,
            uow.session,
            parent_type="Scene",
            parent_id=body.scene_id,
        )

    command = ConfirmRevisionCommand(
        project_id=body.project_id,
        scene_id=body.scene_id,
        shot_code=body.shot_code,
        description=body.description,
        narrative_intent=body.narrative_intent,
        location_ref=body.location_ref,
        basis_script_revision_id=body.basis_script_revision_id,
        script_anchors=tuple(body.script_anchors),
        character_refs=tuple(body.character_refs),
        prop_refs=tuple(body.prop_refs),
        constraint_pack_applicability=body.constraint_pack_applicability,
        constraint_pack_id=body.constraint_pack_id,
        creative_autonomy=body.creative_autonomy,
        risk=body.risk,
        quality_profile=body.quality_profile,
        intent_ref=body.intent_ref,
        source_draft_ref=body.source_draft_ref,
        source_candidate_refs=tuple(body.source_candidate_refs),
        change_origin=body.change_origin,
        change_summary=body.change_summary,
        changed_fields=tuple(body.changed_fields),
        provenance=body.provenance,
        shot_id=body.shot_id,
        expected_shot_revision_id=body.expected_shot_revision_id,
    )
    shot, revision = await service.confirm_revision(
        command, confirmed_by=principal.subject_id, tenant_id=principal.tenant_id
    )
    await uow.commit()
    return {"shot": shot.to_dict(), "shot_revision": revision.to_dict()}


@router.post("/{entity_id}/status")
async def change_shot_status(
    entity_id: str,
    body: ChangeShotStatusRequest,
    principal: Principal = Depends(
        require_resource_role(
            "SHOT.STATUS.CHANGE",
            resource_type="Shot",
            param_name="entity_id",
            uow_dep=get_project_tenant_uow,
        )
    ),
    uow: UnitOfWork = Depends(get_project_tenant_uow),
    service: ShotService = Depends(_get_service),
) -> dict[str, Any]:
    """推进镜头状态；守卫字段与终态语义由领域状态机判定。"""
    shot = await service.change_status(
        entity_id=entity_id,
        target_status=body.target_status,
        changed_by=principal.subject_id,
        tenant_id=principal.tenant_id,
        blocked_reason=body.blocked_reason,
        rework_basis_ref=body.rework_basis_ref,
        cancellation_ref=body.cancellation_ref,
        cancelled_at=body.cancelled_at,
    )
    await uow.commit()
    return shot.to_dict()


__all__ = ["_ShotResponse", "_ShotRevisionResponse", "router"]
