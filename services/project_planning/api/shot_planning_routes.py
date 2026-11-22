"""Scene / Shot / ShotBreakdownDraft / ReworkTask API 路由（薄层）。

为什么四个 router 在同一个模块：它们的服务装配相互依赖（确认草案要同时用
Scene / Shot / ShotRevision / Draft 四个仓储），拆成四个文件会让同一张装配图
被复制四遍。路由本身仍是四个独立 router，main 逐个 include。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

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
from services.project_planning.domain.enums import (
    ConstraintPackStatus,
    ReworkTaskStatus,
    SceneStatus,
    ShotStatus,
)
from services.project_planning.repository.episode_sqlalchemy_repo import (
    SqlAlchemyEpisodeRepository,
)
from services.project_planning.repository.shot_planning_sqlalchemy_repos import (
    SqlAlchemyConstraintPackRepository,
    SqlAlchemyReworkTaskRepository,
    SqlAlchemySceneRepository,
    SqlAlchemyShotDraftRepository,
    SqlAlchemyShotRepository,
    SqlAlchemyShotRevisionRepository,
    SqlAlchemyShotSpecRepository,
)
from services.project_planning.repository.sqlalchemy_outbox_writer import (
    SqlAlchemyOutboxWriter,
)
from services.project_planning.repository.sqlalchemy_repo import (
    SqlAlchemyProjectRepository,
)
from services.project_planning.service.constraint_pack_service import (
    ConstraintPackService,
)
from services.project_planning.service.rework_task_service import ReworkTaskService
from services.project_planning.service.scene_service import SceneService
from services.project_planning.service.shot_draft_service import ShotDraftService
from services.project_planning.service.shot_service import ShotService
from services.project_planning.service.shot_spec_service import ShotSpecService

scene_router = APIRouter(prefix="/scenes", tags=["scene-management"])
shot_router = APIRouter(prefix="/shots", tags=["shot-management"])
draft_router = APIRouter(prefix="/shot-drafts", tags=["shot-breakdown-draft"])
rework_router = APIRouter(prefix="/rework-tasks", tags=["rework-task"])
shot_spec_router = APIRouter(prefix="/shot-specs", tags=["shot-spec"])
constraint_pack_router = APIRouter(
    prefix="/constraint-packs", tags=["constraint-pack"]
)

_SceneResponse = contract_model("Scene")
_ShotResponse = contract_model("Shot")
_DraftResponse = contract_model("ShotBreakdownDraft")
_ReworkResponse = contract_model("ReworkTask")
_ShotSpecResponse = contract_model("ShotSpec")
_ConstraintPackResponse = contract_model("ConstraintPack")


# --------------------------------------------------------------------------- #
# 请求模型
# --------------------------------------------------------------------------- #
class CreateSceneRequest(BaseModel):
    project_id: str = Field(..., min_length=1, max_length=64)
    episode_id: str = Field(..., min_length=1, max_length=64)
    scene_code: str = Field(..., min_length=1, max_length=64)
    entity_id: str | None = None


class ChangeSceneStatusRequest(BaseModel):
    target_status: SceneStatus


class ChangeShotStatusRequest(BaseModel):
    """镜头状态变更。

    四个证据字段是**可选入参但条件必填**：进入 BLOCKED 必须给 blocked_reason；
    取消必须给 cancellation_ref + cancelled_at；返工回退必须给 rework_basis_ref。
    条件必填由领域层判定，请求模型只负责类型。
    """

    target_status: ShotStatus
    blocked_reason: str | None = Field(default=None, max_length=2000)
    rework_basis_ref: str | None = Field(default=None, max_length=64)
    cancellation_ref: str | None = Field(default=None, max_length=128)
    cancelled_at: datetime | None = None


class CreateDraftRequest(BaseModel):
    project_id: str = Field(..., min_length=1, max_length=64)
    basis_script_revision_id: str = Field(..., min_length=1, max_length=64)
    plan_name: str = Field(..., min_length=1, max_length=200)
    generation_method: str = Field(..., min_length=1, max_length=64)
    breakdown_scope: dict[str, Any]
    scene_breakdowns: list[Any] = Field(..., min_length=1)
    episode_ref: str | None = Field(default=None, max_length=64)
    supersedes_draft_ref: str | None = Field(default=None, max_length=64)
    change_summary: str | None = None
    ai_rationale: str | None = None
    open_questions: list[Any] | None = None
    potential_issues: list[Any] | None = None
    entity_id: str | None = None


class ReturnDraftRequest(BaseModel):
    return_reason: str = Field(..., min_length=1)


class DiscardDraftRequest(BaseModel):
    discarded_reason: str = Field(..., min_length=1)


class CreateReworkTaskRequest(BaseModel):
    project_id: str = Field(..., min_length=1, max_length=64)
    issue_id: str = Field(..., min_length=1, max_length=64)
    target_ref: str = Field(..., min_length=1, max_length=64)
    defect_category: str = Field(..., min_length=1, max_length=64)
    severity: str = Field(..., min_length=1, max_length=32)
    root_cause: str = Field(..., min_length=1)
    return_node: str = Field(..., min_length=1, max_length=64)
    affected_asset_version_ids: list[str] = Field(default_factory=list)
    downstream_task_ids: list[str] = Field(default_factory=list)
    constraint_pack_ref: str | None = Field(default=None, max_length=64)
    entity_id: str | None = None


class ChangeReworkStatusRequest(BaseModel):
    target_status: ReworkTaskStatus


class CreateShotSpecRequest(BaseModel):
    """创建规格（8.12）：绑定精确修订，SS-3 由领域层与库层各判一次。

    ``fps`` 在请求体里拆成 ``fps_num`` / ``fps_den`` 两个整数：契约的
    ``{num, den}`` 是**表示**，浮点帧率（29.97）会被 30fps 与 30000/1001 同时
    接受，而它们对应的帧数不同——把整数形态作为唯一入口，浮点根本没有机会进来。
    """

    project_id: str = Field(..., min_length=1, max_length=64)
    shot_revision_id: str = Field(..., min_length=1, max_length=64)
    duration_ms: int = Field(..., ge=1)
    fps_num: int = Field(..., ge=1)
    fps_den: int = Field(..., ge=1)
    cut_points_frames: list[int] = Field(..., min_length=1)
    # 描述性字段不设 min_length：契约只声明 ``type: string``，DRAFT 阶段
    # 「尚未决定」与「无尺寸变化」都是空串。在这里要求非空，就是把一个契约
    # 没有的格式变成 422——合法输入被拒，且调用方无法从契约里读到原因。
    aspect_ratio: str = Field("", max_length=32)
    coordinate_system: str = Field("", max_length=32)
    camera_path: str = Field("", max_length=4000)
    subject_paths: str = Field("", max_length=4000)
    shot_size_changes: str = Field("", max_length=500)
    previz_ref: str = Field("", max_length=200)
    control_signal_refs: list[str] | None = None
    entity_id: str | None = None


class LockShotSpecRequest(BaseModel):
    """锁定规格：维度与依据都是条件必填（缺一即拒，在服务层判定）。"""

    locked_dimensions: str = Field(..., min_length=1, max_length=512)
    lock_basis_ref: str = Field(..., min_length=1, max_length=128)


# --------------------------------------------------------------------------- #
# 服务装配
# --------------------------------------------------------------------------- #
def _scene_service(session: AsyncSession) -> SceneService:
    return SceneService(
        SqlAlchemySceneRepository(session),
        SqlAlchemyEpisodeRepository(session),
        SqlAlchemyOutboxWriter(session),
        # 两条守卫依赖都必须装配：镜头仓储承载 SC-5 归档前置条件，
        # 父项目仓储承载父链冻结判定（两条寻址线都要用）。
        SqlAlchemyShotRepository(session),
        SqlAlchemyProjectRepository(session),
    )


def _shot_service(session: AsyncSession) -> ShotService:
    return ShotService(
        SqlAlchemyShotRepository(session),
        SqlAlchemyReworkTaskRepository(session),
        # 镜头进入在产前要持父场次的共享行锁（SC-5 的并发侧），
        # 与 SceneService 归档侧的排他锁配对，缺任一侧屏障即失效。
        SqlAlchemySceneRepository(session),
        SqlAlchemyOutboxWriter(session),
    )


def _draft_service(session: AsyncSession) -> ShotDraftService:
    return ShotDraftService(
        SqlAlchemyShotDraftRepository(session),
        SqlAlchemyShotRepository(session),
        SqlAlchemyShotRevisionRepository(session),
        SqlAlchemySceneRepository(session),
        SqlAlchemyProjectRepository(session),
        SqlAlchemyOutboxWriter(session),
    )


def _rework_service(session: AsyncSession) -> ReworkTaskService:
    return ReworkTaskService(
        SqlAlchemyReworkTaskRepository(session),
        SqlAlchemyProjectRepository(session),
        SqlAlchemyOutboxWriter(session),
    )


def _spec_service(session: AsyncSession) -> ShotSpecService:
    return ShotSpecService(
        SqlAlchemyShotSpecRepository(session),
        # 规格绑定的修订必须真实存在且同项目：缺这个仓储，SS-2 就只剩一句注释。
        SqlAlchemyShotRevisionRepository(session),
        SqlAlchemyProjectRepository(session),
        SqlAlchemyOutboxWriter(session),
    )


def _get_scene(uow: UnitOfWork = Depends(get_project_tenant_uow)) -> SceneService:
    return _scene_service(uow.session)


def _get_read_scene(uow: UnitOfWork = Depends(get_read_project_tenant_uow)) -> SceneService:
    return _scene_service(uow.session)


def _get_shot(uow: UnitOfWork = Depends(get_project_tenant_uow)) -> ShotService:
    return _shot_service(uow.session)


def _get_read_shot(uow: UnitOfWork = Depends(get_read_project_tenant_uow)) -> ShotService:
    return _shot_service(uow.session)


def _get_draft(uow: UnitOfWork = Depends(get_project_tenant_uow)) -> ShotDraftService:
    return _draft_service(uow.session)


def _get_read_draft(uow: UnitOfWork = Depends(get_read_project_tenant_uow)) -> ShotDraftService:
    return _draft_service(uow.session)


def _get_rework(uow: UnitOfWork = Depends(get_project_tenant_uow)) -> ReworkTaskService:
    return _rework_service(uow.session)


def _get_read_rework(uow: UnitOfWork = Depends(get_read_project_tenant_uow)) -> ReworkTaskService:
    return _rework_service(uow.session)


def _get_spec(uow: UnitOfWork = Depends(get_project_tenant_uow)) -> ShotSpecService:
    return _spec_service(uow.session)


def _get_read_spec(
    uow: UnitOfWork = Depends(get_read_project_tenant_uow),
) -> ShotSpecService:
    return _spec_service(uow.session)


# --------------------------------------------------------------------------- #
# Scene
# --------------------------------------------------------------------------- #
@scene_router.post("", status_code=201, response_model=_SceneResponse)
async def create_scene(
    body: CreateSceneRequest,
    principal: Principal = Depends(require_role("SCENE.CREATE")),
    uow: UnitOfWork = Depends(get_project_tenant_uow),
    service: SceneService = Depends(_get_scene),
) -> dict[str, Any]:
    """登记场次。归属判定对象是父集数（场次尚不存在）。"""
    await authorize_on_parent(
        "SCENE.CREATE",
        principal,
        uow.session,
        parent_type="Episode",
        parent_id=body.episode_id,
    )
    scene = await service.create_scene(
        project_id=body.project_id,
        episode_id=body.episode_id,
        scene_code=body.scene_code,
        created_by=principal.subject_id,
        tenant_id=principal.tenant_id,
        entity_id=body.entity_id,
    )
    await uow.commit()
    return scene.to_dict()


@scene_router.get("/{entity_id}", response_model=_SceneResponse)
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
    service: SceneService = Depends(_get_read_scene),
) -> dict[str, Any]:
    scene = await service.get_scene(entity_id)
    return scene.to_dict()


@scene_router.post("/{entity_id}/status", response_model=_SceneResponse)
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
    service: SceneService = Depends(_get_scene),
) -> dict[str, Any]:
    scene = await service.change_status(
        entity_id=entity_id,
        target_status=body.target_status,
        changed_by=principal.subject_id,
        tenant_id=principal.tenant_id,
    )
    await uow.commit()
    return scene.to_dict()


# --------------------------------------------------------------------------- #
# Shot
# --------------------------------------------------------------------------- #
@shot_router.get("/{entity_id}", response_model=_ShotResponse)
async def get_shot(
    entity_id: str,
    _principal: Principal = Depends(
        require_resource_role(
            "SHOT.READ",
            resource_type="Shot",
            param_name="entity_id",
            uow_dep=get_read_project_tenant_uow,
        )
    ),
    service: ShotService = Depends(_get_read_shot),
) -> dict[str, Any]:
    shot = await service.get_shot(entity_id)
    return shot.to_dict()


@shot_router.post("/{entity_id}/status", response_model=_ShotResponse)
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
    service: ShotService = Depends(_get_shot),
) -> dict[str, Any]:
    """推进镜头状态；6 条守卫边的条件必填在领域层判定。"""
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


# --------------------------------------------------------------------------- #
# ShotBreakdownDraft
# --------------------------------------------------------------------------- #
@draft_router.post("", status_code=201, response_model=_DraftResponse)
async def create_draft(
    body: CreateDraftRequest,
    principal: Principal = Depends(require_role("SHOT.DRAFT.CREATE")),
    uow: UnitOfWork = Depends(get_project_tenant_uow),
    service: ShotDraftService = Depends(_get_draft),
) -> dict[str, Any]:
    """AI 拆镜产出草案。归属判定对象是父项目（草案尚不存在）。"""
    await authorize_on_parent(
        "SHOT.DRAFT.CREATE",
        principal,
        uow.session,
        parent_type="Project",
        parent_id=body.project_id,
    )
    draft = await service.create_draft(
        project_id=body.project_id,
        basis_script_revision_id=body.basis_script_revision_id,
        plan_name=body.plan_name,
        generation_method=body.generation_method,
        breakdown_scope=body.breakdown_scope,
        scene_breakdowns=body.scene_breakdowns,
        created_by=principal.subject_id,
        tenant_id=principal.tenant_id,
        episode_ref=body.episode_ref,
        supersedes_draft_ref=body.supersedes_draft_ref,
        change_summary=body.change_summary,
        ai_rationale=body.ai_rationale,
        open_questions=body.open_questions,
        potential_issues=body.potential_issues,
        entity_id=body.entity_id,
    )
    await uow.commit()
    return draft.to_dict()


@draft_router.get("/{entity_id}", response_model=_DraftResponse)
async def get_draft(
    entity_id: str,
    _principal: Principal = Depends(
        require_resource_role(
            "SHOT.DRAFT.READ",
            resource_type="ShotBreakdownDraft",
            param_name="entity_id",
            uow_dep=get_read_project_tenant_uow,
        )
    ),
    service: ShotDraftService = Depends(_get_read_draft),
) -> dict[str, Any]:
    draft = await service.get_draft(entity_id)
    return draft.to_dict()


@draft_router.post("/{entity_id}/confirm", response_model=_DraftResponse)
async def confirm_draft(
    entity_id: str,
    principal: Principal = Depends(
        require_resource_role(
            "SHOT.DRAFT.CONFIRM",
            resource_type="ShotBreakdownDraft",
            param_name="entity_id",
            uow_dep=get_project_tenant_uow,
        )
    ),
    # 同一端点上第二个动作也必须独立过授权：这个请求既落草案终态，
    # 又创建 Shot + 首个 ShotRevision。少判一次，等于让「能确认草案」
    # 隐式获得「能建镜头」。
    _revision_principal: Principal = Depends(
        require_resource_role(
            "SHOT.REVISION.CONFIRM",
            resource_type="ShotBreakdownDraft",
            param_name="entity_id",
            uow_dep=get_project_tenant_uow,
        )
    ),
    uow: UnitOfWork = Depends(get_project_tenant_uow),
    service: ShotDraftService = Depends(_get_draft),
) -> dict[str, Any]:
    """确认拆镜：认领 → 落定 → 原子创建 Shot + 首个 ShotRevision。"""
    draft = await service.confirm_draft(
        entity_id=entity_id,
        confirmed_by=principal.subject_id,
        tenant_id=principal.tenant_id,
    )
    await uow.commit()
    return draft.to_dict()


@draft_router.post("/{entity_id}/return", response_model=_DraftResponse)
async def return_draft(
    entity_id: str,
    body: ReturnDraftRequest,
    principal: Principal = Depends(
        require_resource_role(
            "SHOT.DRAFT.RETURN",
            resource_type="ShotBreakdownDraft",
            param_name="entity_id",
            uow_dep=get_project_tenant_uow,
        )
    ),
    uow: UnitOfWork = Depends(get_project_tenant_uow),
    service: ShotDraftService = Depends(_get_draft),
) -> dict[str, Any]:
    draft = await service.return_draft(
        entity_id=entity_id,
        returned_by=principal.subject_id,
        return_reason=body.return_reason,
        tenant_id=principal.tenant_id,
    )
    await uow.commit()
    return draft.to_dict()


@draft_router.post("/{entity_id}/discard", response_model=_DraftResponse)
async def discard_draft(
    entity_id: str,
    body: DiscardDraftRequest,
    principal: Principal = Depends(
        require_resource_role(
            "SHOT.DRAFT.DISCARD",
            resource_type="ShotBreakdownDraft",
            param_name="entity_id",
            uow_dep=get_project_tenant_uow,
        )
    ),
    uow: UnitOfWork = Depends(get_project_tenant_uow),
    service: ShotDraftService = Depends(_get_draft),
) -> dict[str, Any]:
    draft = await service.discard_draft(
        entity_id=entity_id,
        discarded_reason=body.discarded_reason,
        changed_by=principal.subject_id,
        tenant_id=principal.tenant_id,
    )
    await uow.commit()
    return draft.to_dict()


# --------------------------------------------------------------------------- #
# ReworkTask
# --------------------------------------------------------------------------- #
@rework_router.post("", status_code=201, response_model=_ReworkResponse)
async def create_rework_task(
    body: CreateReworkTaskRequest,
    principal: Principal = Depends(require_role("REWORK.TASK.CREATE")),
    uow: UnitOfWork = Depends(get_project_tenant_uow),
    service: ReworkTaskService = Depends(_get_rework),
) -> dict[str, Any]:
    await authorize_on_parent(
        "REWORK.TASK.CREATE",
        principal,
        uow.session,
        parent_type="Project",
        parent_id=body.project_id,
    )
    task = await service.create_task(
        project_id=body.project_id,
        issue_id=body.issue_id,
        target_ref=body.target_ref,
        defect_category=body.defect_category,
        severity=body.severity,
        root_cause=body.root_cause,
        return_node=body.return_node,
        affected_asset_version_ids=body.affected_asset_version_ids,
        downstream_task_ids=body.downstream_task_ids,
        created_by=principal.subject_id,
        tenant_id=principal.tenant_id,
        constraint_pack_ref=body.constraint_pack_ref,
        entity_id=body.entity_id,
    )
    await uow.commit()
    return task.to_dict()


@rework_router.get("/{entity_id}", response_model=_ReworkResponse)
async def get_rework_task(
    entity_id: str,
    _principal: Principal = Depends(
        require_resource_role(
            "REWORK.TASK.READ",
            resource_type="ReworkTask",
            param_name="entity_id",
            uow_dep=get_read_project_tenant_uow,
        )
    ),
    service: ReworkTaskService = Depends(_get_read_rework),
) -> dict[str, Any]:
    task = await service.get_task(entity_id)
    return task.to_dict()


@rework_router.post("/{entity_id}/status", response_model=_ReworkResponse)
async def change_rework_status(
    entity_id: str,
    body: ChangeReworkStatusRequest,
    principal: Principal = Depends(
        require_resource_role(
            "REWORK.TASK.STATUS.CHANGE",
            resource_type="ReworkTask",
            param_name="entity_id",
            uow_dep=get_project_tenant_uow,
        )
    ),
    uow: UnitOfWork = Depends(get_project_tenant_uow),
    service: ReworkTaskService = Depends(_get_rework),
) -> dict[str, Any]:
    task = await service.change_status(
        entity_id=entity_id,
        target_status=body.target_status,
        changed_by=principal.subject_id,
        tenant_id=principal.tenant_id,
    )
    await uow.commit()
    return task.to_dict()


# --------------------------------------------------------------------------- #
# ShotSpec
# --------------------------------------------------------------------------- #
@shot_spec_router.post("", status_code=201, response_model=_ShotSpecResponse)
async def create_shot_spec(
    body: CreateShotSpecRequest,
    principal: Principal = Depends(require_role("SHOTSPEC.CREATE")),
    uow: UnitOfWork = Depends(get_project_tenant_uow),
    service: ShotSpecService = Depends(_get_spec),
) -> dict[str, Any]:
    """登记规格。归属判定对象是父项目（规格尚不存在）。"""
    await authorize_on_parent(
        "SHOTSPEC.CREATE",
        principal,
        uow.session,
        parent_type="Project",
        parent_id=body.project_id,
    )
    spec = await service.create_spec(
        project_id=body.project_id,
        shot_revision_id=body.shot_revision_id,
        duration_ms=body.duration_ms,
        fps_num=body.fps_num,
        fps_den=body.fps_den,
        cut_points_frames=body.cut_points_frames,
        aspect_ratio=body.aspect_ratio,
        coordinate_system=body.coordinate_system,
        camera_path=body.camera_path,
        subject_paths=body.subject_paths,
        shot_size_changes=body.shot_size_changes,
        previz_ref=body.previz_ref,
        control_signal_refs=body.control_signal_refs,
        created_by=principal.subject_id,
        tenant_id=principal.tenant_id,
        entity_id=body.entity_id,
    )
    await uow.commit()
    return spec.to_dict()


@shot_spec_router.get("/{entity_id}", response_model=_ShotSpecResponse)
async def get_shot_spec(
    entity_id: str,
    _principal: Principal = Depends(
        require_resource_role(
            "SHOTSPEC.READ",
            resource_type="ShotSpec",
            param_name="entity_id",
            uow_dep=get_read_project_tenant_uow,
        )
    ),
    service: ShotSpecService = Depends(_get_read_spec),
) -> dict[str, Any]:
    spec = await service.get_spec(entity_id)
    return spec.to_dict()


@shot_spec_router.post("/{entity_id}/lock", response_model=_ShotSpecResponse)
async def lock_shot_spec(
    entity_id: str,
    body: LockShotSpecRequest,
    principal: Principal = Depends(
        require_resource_role(
            "SHOTSPEC.LOCK",
            resource_type="ShotSpec",
            param_name="entity_id",
            uow_dep=get_project_tenant_uow,
        )
    ),
    uow: UnitOfWork = Depends(get_project_tenant_uow),
    service: ShotSpecService = Depends(_get_spec),
) -> dict[str, Any]:
    """锁定规格：维度 / 依据 / 版本 / 审批事件四要素齐备才落 LOCKED。"""
    spec = await service.lock_spec(
        entity_id=entity_id,
        locked_dimensions=body.locked_dimensions,
        lock_basis_ref=body.lock_basis_ref,
        locked_by=principal.subject_id,
        tenant_id=principal.tenant_id,
    )
    await uow.commit()
    return spec.to_dict()


# --------------------------------------------------------------------------- #
# ConstraintPack（A23）
# --------------------------------------------------------------------------- #
class CreateConstraintPackRequest(BaseModel):
    """登记约束包（7.9）。

    ``applicability`` 用 ``Literal`` 而不是自由字符串：它是 7.9 明文枚举
    （APPLICABLE / NOT_APPLICABLE），且「无锁定约束必须显式标 NOT_APPLICABLE，
    不得留空」——留空在这里就是 422，而不是等到领域层再拒绝。
    领域层仍是权威（它给出的错误文案能说清「为什么不能留空」），表示层只是提前。

    ``locked_dimensions`` / ``constraints`` 默认空：DRAFT 阶段的包可以渐进填写，
    内容完备性判定挂在 DRAFT→ACTIVE 这条边上（见 domain/constraint_pack.py）。
    """

    project_id: str = Field(..., min_length=1, max_length=64)
    shot_ids: list[str] = Field(..., min_length=1)
    applicability: Literal["APPLICABLE", "NOT_APPLICABLE"]
    scene_id: str = Field("", max_length=64)
    shot_group_ids: list[str] | None = None
    locked_dimensions: list[str] | None = None
    constraints: list[dict[str, Any]] | None = None
    source_previz_revision: str = Field("", max_length=200)
    version: int = Field(1, ge=1)
    # 显式给出表示「同一逻辑包的另一个版本」；缺省时由服务层取实体 id。
    constraint_pack_id: str | None = Field(default=None, max_length=64)
    entity_id: str | None = None


class LockConstraintPackRequest(BaseModel):
    """使约束包生效（DRAFT → ACTIVE）。

    ``approved_at`` 是**审批事实**而非请求时间，故要求调用方显式给出：
    服务端替审批人编一个时间，等于替别人声明了一件他没声明的事。
    ``lock_basis_ref`` 写进迁移事实的 ``authority_ref``（契约里没有包级依据字段，
    但它必须落在不可篡改的地方，而不是只出现在事件 payload 里）。
    """

    approved_at: datetime
    lock_basis_ref: str = Field(..., min_length=1, max_length=128)


def _pack_service(session: AsyncSession) -> ConstraintPackService:
    return ConstraintPackService(
        SqlAlchemyConstraintPackRepository(session),
        # 作用镜头必须真实存在且同项目：缺这个仓储，「约束包只能约束本项目
        # 的镜头」就只剩库层触发器一句 23503。
        SqlAlchemyShotRepository(session),
        SqlAlchemyProjectRepository(session),
        SqlAlchemyOutboxWriter(session),
    )


def _get_pack(uow: UnitOfWork = Depends(get_project_tenant_uow)) -> ConstraintPackService:
    return _pack_service(uow.session)


def _get_read_pack(
    uow: UnitOfWork = Depends(get_read_project_tenant_uow),
) -> ConstraintPackService:
    return _pack_service(uow.session)


@constraint_pack_router.post("", status_code=201, response_model=_ConstraintPackResponse)
async def create_constraint_pack(
    body: CreateConstraintPackRequest,
    principal: Principal = Depends(require_role("CONSTRAINT.PACK.CREATE")),
    uow: UnitOfWork = Depends(get_project_tenant_uow),
    service: ConstraintPackService = Depends(_get_pack),
) -> dict[str, Any]:
    """登记约束包。归属判定对象是父项目（包尚不存在）。"""
    await authorize_on_parent(
        "CONSTRAINT.PACK.CREATE",
        principal,
        uow.session,
        parent_type="Project",
        parent_id=body.project_id,
    )
    pack = await service.create_pack(
        project_id=body.project_id,
        shot_ids=body.shot_ids,
        applicability=body.applicability,
        scene_id=body.scene_id,
        shot_group_ids=body.shot_group_ids,
        locked_dimensions=body.locked_dimensions,
        constraints=body.constraints,
        source_previz_revision=body.source_previz_revision,
        version=body.version,
        constraint_pack_id=body.constraint_pack_id,
        created_by=principal.subject_id,
        tenant_id=principal.tenant_id,
        entity_id=body.entity_id,
    )
    await uow.commit()
    return pack.to_dict()


@constraint_pack_router.get("/{entity_id}", response_model=_ConstraintPackResponse)
async def get_constraint_pack(
    entity_id: str,
    _principal: Principal = Depends(
        require_resource_role(
            "CONSTRAINT.PACK.READ",
            resource_type="ConstraintPack",
            param_name="entity_id",
            uow_dep=get_read_project_tenant_uow,
        )
    ),
    service: ConstraintPackService = Depends(_get_read_pack),
) -> dict[str, Any]:
    pack = await service.get_pack(entity_id)
    return pack.to_dict()


@constraint_pack_router.post(
    "/{entity_id}/lock", response_model=_ConstraintPackResponse
)
async def lock_constraint_pack(
    entity_id: str,
    body: LockConstraintPackRequest,
    principal: Principal = Depends(
        require_resource_role(
            "CONSTRAINT.PACK.LOCK",
            resource_type="ConstraintPack",
            param_name="entity_id",
            uow_dep=get_project_tenant_uow,
        )
    ),
    uow: UnitOfWork = Depends(get_project_tenant_uow),
    service: ConstraintPackService = Depends(_get_pack),
) -> dict[str, Any]:
    """使约束包生效：审批人 / 审批时间 / 依据 / 审批事件四要素齐备才落 ACTIVE。"""
    pack = await service.lock_pack(
        entity_id=entity_id,
        approved_by=principal.subject_id,
        approved_at=body.approved_at,
        lock_basis_ref=body.lock_basis_ref,
        tenant_id=principal.tenant_id,
    )
    await uow.commit()
    return pack.to_dict()


class ChangeConstraintPackStatusRequest(BaseModel):
    """推进治理边（ACTIVE → REVOKED / STALE / SUPERSEDED）。

    目标状态决定哪一个依据字段必填（服务层校验）；其余依据留空。三条边都
    不得用于内容修改——内容变更必须新建版本并经 LOCK。
    """

    target_status: ConstraintPackStatus
    revocation_basis_ref: str = Field("", max_length=128)
    stale_source_ref: str = Field("", max_length=128)
    superseding_pack_ref: str = Field("", max_length=128)


@constraint_pack_router.post(
    "/{entity_id}/status", response_model=_ConstraintPackResponse
)
async def change_constraint_pack_status(
    entity_id: str,
    body: ChangeConstraintPackStatusRequest,
    principal: Principal = Depends(
        require_resource_role(
            "CONSTRAINT.PACK.STATUS.CHANGE",
            resource_type="ConstraintPack",
            param_name="entity_id",
            uow_dep=get_project_tenant_uow,
        )
    ),
    uow: UnitOfWork = Depends(get_project_tenant_uow),
    service: ConstraintPackService = Depends(_get_pack),
) -> dict[str, Any]:
    """推进约束包治理边。依据必填字段由目标状态决定（见请求模型）。"""
    pack = await service.change_status(
        entity_id=entity_id,
        target_status=body.target_status,
        changed_by=principal.subject_id,
        tenant_id=principal.tenant_id,
        revocation_basis_ref=body.revocation_basis_ref,
        stale_source_ref=body.stale_source_ref,
        superseding_pack_ref=body.superseding_pack_ref,
    )
    await uow.commit()
    return pack.to_dict()
