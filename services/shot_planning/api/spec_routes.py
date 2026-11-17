"""ShotSpec API 路由（薄层，只做协议适配与 DTO 转换）。

契约对齐：
- SHOTSPEC.LOCK：授权锁定规格维度（resource_type=ShotSpec，HIGH，导演/授权 TD）
- 规格创建是内部领域用例（无冻结授权动作），不暴露公共端点（fail-closed）
- 请求/响应走统一信封与错误信封；业务逻辑下沉到 ShotSpecService。
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
    get_shot_uow,
    require_action,
)
from services.shot_planning.repository.shot_revision_sqlalchemy_repo import (
    SqlAlchemyShotRevisionRepository,
)
from services.shot_planning.repository.shot_spec_sqlalchemy_repo import (
    SqlAlchemyShotSpecRepository,
)
from services.shot_planning.service.spec_service import ShotSpecService

router = APIRouter(prefix="/shots/specs", tags=["shot-planning"])

# 响应模型由契约定义生成（R3）：字段集恒等于 object_schemas 的对应定义。
_ShotSpecResponse = contract_model("ShotSpec")


class LockShotSpecRequest(BaseModel):
    """锁定规格维度请求体（身份不在请求体，由 Principal 提供）。"""

    project_id: str = Field(..., min_length=1, max_length=64)
    locked_dimensions: list[str] = Field(..., min_length=1)
    rationale: str = Field(..., min_length=1)
    version: int = Field(..., ge=1, description="请求方已知的规格版本（乐观锁）")


def _build_service(session: AsyncSession) -> ShotSpecService:
    """服务装配：RLS 会话 + spec/revision 仓储端口 + Outbox 写入端口。"""
    return ShotSpecService(
        SqlAlchemyShotSpecRepository(session),
        SqlAlchemyShotRevisionRepository(session),
        SqlAlchemyOutboxWriter(session),
    )


def _get_service(uow: UnitOfWork = Depends(get_shot_uow)) -> ShotSpecService:
    """写用例服务装配（提交点由路由显式控制）。"""
    return _build_service(uow.session)


@router.post("/{spec_id}/lock", response_model=_ShotSpecResponse)
async def lock_shot_spec(
    spec_id: str,
    body: LockShotSpecRequest,
    principal: Principal = Depends(require_action("SHOTSPEC.LOCK")),
    uow: UnitOfWork = Depends(get_shot_uow),
    service: ShotSpecService = Depends(_get_service),
) -> dict[str, Any]:
    """授权锁定规格维度（锁定维度/依据/版本和审批事件齐备）。"""
    authorize(
        principal,
        "SHOTSPEC.LOCK",
        resource=ResourceRef(resource_type="ShotSpec", project_id=body.project_id),
    )
    spec = await service.lock_spec(
        spec_id=spec_id,
        project_id=body.project_id,
        dimensions=body.locked_dimensions,
        rationale=body.rationale,
        version=body.version,
        principal=principal,
    )
    await uow.commit()
    return spec.to_dict()
