"""ConstraintPack API 路由（薄层，只做协议适配与 DTO 转换）。

契约对齐（7.9 / ADR-088）：
- CONSTRAINT.PACK.CREATE：创建约束包草案（MEDIUM，Agent/TD/导演）；
  Agent 仅能创建 DRAFT；无锁定约束显式标 NOT_APPLICABLE
- CONSTRAINT.PACK.LOCK：授权激活约束包（HIGH，导演/授权 TD）；
  仅锁定经批准的维度；审批与版本固定
- 请求/响应走统一信封与错误信封；业务逻辑下沉到 ConstraintPackService。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.identity.authorization import ResourceRef
from packages.identity.enforcement import authorize
from packages.shared.contract_models import contract_model
from packages.shared.outbox_writer import SqlAlchemyOutboxWriter
from packages.shared.unit_of_work import UnitOfWork
from services.identity_access.deps import (
    Principal,
    get_shot_uow,
    require_action,
)
from services.shot_planning.domain.enums import ConstraintPackApplicability
from services.shot_planning.repository.constraint_pack_sqlalchemy_repo import (
    SqlAlchemyConstraintPackRepository,
)
from services.shot_planning.service.constraint_pack_service import (
    ConstraintPackService,
)

router = APIRouter(prefix="/shots/constraint-packs", tags=["shot-planning"])

# 响应模型由契约定义生成（R3）：字段集恒等于 object_schemas 的对应定义。
_ConstraintPackResponse = contract_model("ConstraintPack")


class CreateConstraintPackRequest(BaseModel):
    """创建约束包草案请求体（身份不在请求体，由 Principal 提供）。"""

    project_id: str = Field(..., min_length=1, max_length=64)
    shot_ids: list[str] = Field(..., min_length=1)
    applicability: str = Field(..., description="APPLICABLE / NOT_APPLICABLE")
    locked_dimensions: list[str] | None = None
    constraints: list[dict[str, Any]] | None = None
    scene_id: str | None = Field(None, max_length=64)
    shot_group_ids: list[str] | None = None
    source_previz_revision: str | None = Field(None, max_length=64)
    version: int = Field(1, ge=1)
    entity_id: str | None = None


class LockConstraintPackRequest(BaseModel):
    """激活约束包请求体。"""

    project_id: str = Field(..., min_length=1, max_length=64)


def _build_service(session: AsyncSession) -> ConstraintPackService:
    """服务装配：RLS 会话 + pack 仓储端口 + Outbox 写入端口。"""
    return ConstraintPackService(
        SqlAlchemyConstraintPackRepository(session),
        SqlAlchemyOutboxWriter(session),
    )


def _get_service(uow: UnitOfWork = Depends(get_shot_uow)) -> ConstraintPackService:
    """写用例服务装配（提交点由路由显式控制）。"""
    return _build_service(uow.session)


@router.post("", status_code=201, response_model=_ConstraintPackResponse)
async def create_constraint_pack(
    body: CreateConstraintPackRequest,
    principal: Principal = Depends(require_action("CONSTRAINT.PACK.CREATE")),
    uow: UnitOfWork = Depends(get_shot_uow),
    service: ConstraintPackService = Depends(_get_service),
) -> dict[str, Any]:
    """创建约束包草案（Agent 仅能创建 DRAFT；无锁定约束标 NOT_APPLICABLE）。"""
    authorize(
        principal,
        "CONSTRAINT.PACK.CREATE",
        resource=ResourceRef(
            resource_type="ConstraintPack", project_id=body.project_id
        ),
    )
    try:
        applicability = ConstraintPackApplicability(body.applicability)
    except ValueError as exc:
        raise DomainError(
            ErrorRegistry.VALIDATION_ERROR,
            detail=(
                f"applicability 非法: {body.applicability!r}"
                "（仅允许 APPLICABLE / NOT_APPLICABLE）"
            ),
        ) from exc
    pack = await service.create_pack(
        project_id=body.project_id,
        shot_ids=body.shot_ids,
        applicability=applicability,
        locked_dimensions=body.locked_dimensions,
        constraints=body.constraints,
        scene_id=body.scene_id,
        shot_group_ids=body.shot_group_ids,
        source_previz_revision=body.source_previz_revision,
        version=body.version,
        principal=principal,
        entity_id=body.entity_id,
    )
    await uow.commit()
    return pack.to_dict()


@router.post("/{pack_id}/lock", response_model=_ConstraintPackResponse)
async def lock_constraint_pack(
    pack_id: str,
    body: LockConstraintPackRequest,
    principal: Principal = Depends(require_action("CONSTRAINT.PACK.LOCK")),
    uow: UnitOfWork = Depends(get_shot_uow),
    service: ConstraintPackService = Depends(_get_service),
) -> dict[str, Any]:
    """授权激活约束包（DRAFT→ACTIVE；审批人与版本固定）。"""
    authorize(
        principal,
        "CONSTRAINT.PACK.LOCK",
        resource=ResourceRef(
            resource_type="ConstraintPack", project_id=body.project_id
        ),
    )
    pack = await service.lock_pack(
        pack_id=pack_id,
        project_id=body.project_id,
        principal=principal,
    )
    await uow.commit()
    return pack.to_dict()
