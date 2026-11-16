"""交付清单 API 路由（薄层，只做协议适配与 DTO 转换）。

契约对齐（contracts/action_registry.yaml）：
- ``DELIVERY.MANIFEST.CREATE`` → ``POST /delivery-manifests``
  （MEDIUM，「交付服务/授权制片」，「仅生成 DRAFT 并绑定完整证据引用」）

只实现 CREATE 的原因：`DELIVERY.MANIFEST.APPROVE`（HIGH，需 Gate4/PictureLock/
权利证据齐备）与 `RELEASE`（CRITICAL）依赖 Gate 决策链，尚未实现。宁可只暴露
一个动作，也不做一个"能改状态"的通用端点——那种端点会让 `status` 的取值变成
调用方决定，等于把状态机交给客户端。
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
from services.asset_service.repository.sqlalchemy_asset_repo import (
    SqlAlchemyAssetRepository,
)
from services.asset_service.repository.sqlalchemy_manifest_repo import (
    SqlAlchemyDeliveryManifestRepository,
)
from services.asset_service.service.delivery_service import DeliveryService
from services.identity_access.deps import (
    Principal,
    get_asset_uow,
    require_action,
)

router = APIRouter(prefix="/delivery-manifests", tags=["delivery"])

#: 重建配方文本长度上限。为 0 不是选项（空配方无从复现）；无上限则等于允许
#: 把清单当成资产存储。4096 足以容纳一份配方 DSL / 定位串。
_MAX_RECIPE_CHARS = 4096

# 响应模型由契约定义生成：字段集永远等于 object_schemas 的 DeliveryManifest。
_DeliveryManifestResponse = contract_model("DeliveryManifest")


class CreateDeliveryManifestRequest(BaseModel):
    """交付清单生成请求体（DELIVERY.MANIFEST.CREATE）。

    刻意**不接受** `checksum` 与 `status`：校验和由服务端按可重建内容计算，
    状态由动作语义决定（只可能是 DRAFT）。允许客户端提交这两项，等于让
    「哈希一致」与「未审批不得放行」两条约束同时失效。
    """

    project_id: str = Field(..., min_length=1, max_length=64)
    spec_ref: str = Field(..., min_length=1, max_length=64)
    qc_final_ref: str = Field(..., min_length=1, max_length=64)
    picture_lock_ref: str = Field(..., min_length=1, max_length=64)
    asset_refs: list[str] = Field(..., min_length=1)
    rights_evidence_refs: list[str] = Field(..., min_length=1)
    # 契约把 rebuild_recipe 声明为 string（见领域模块注释）：此处按不透明配方
    # 文本接收。上限用于阻止把清单当附件存——真正的配方内容应由配方服务托管，
    # 清单只保存可复现的定位/描述。
    rebuild_recipe: str = Field(..., min_length=1, max_length=_MAX_RECIPE_CHARS)
    manifest_id: str | None = Field(default=None, max_length=64)


def _build_service(session: AsyncSession) -> DeliveryService:
    """服务装配：RLS 会话 + 资产/清单仓储端口 + Outbox 写入端口。

    资产仓储是必需的：清单要逐个校验被引用版本的状态与项目归属（同域内查询，
    不涉及跨服务依赖）。
    """
    return DeliveryService(
        SqlAlchemyAssetRepository(session),
        SqlAlchemyDeliveryManifestRepository(session),
        SqlAlchemyOutboxWriter(session),
    )


def _get_service(uow: UnitOfWork = Depends(get_asset_uow)) -> DeliveryService:
    """写用例服务装配（提交点由路由显式控制）。"""
    return _build_service(uow.session)


@router.post("", status_code=201, response_model=_DeliveryManifestResponse)
async def create_delivery_manifest(
    body: CreateDeliveryManifestRequest,
    principal: Principal = Depends(require_action("DELIVERY.MANIFEST.CREATE")),
    uow: UnitOfWork = Depends(get_asset_uow),
    service: DeliveryService = Depends(_get_service),
) -> dict[str, Any]:
    """生成 DRAFT 交付清单（校验资产可交付性 + 服务端计算校验和）。"""
    # 资源级授权（B2）：组织取调用主体，项目取请求体——越界项目必须在这里真判
    # 一次，而不能只依赖"依赖阶段查不到"。
    authorize(
        principal,
        "DELIVERY.MANIFEST.CREATE",
        resource=ResourceRef(
            resource_type="DeliveryManifest",
            organization_id=principal.organization_id,
            project_id=body.project_id,
        ),
    )
    manifest = await service.create_manifest(
        project_id=body.project_id,
        spec_ref=body.spec_ref,
        qc_final_ref=body.qc_final_ref,
        picture_lock_ref=body.picture_lock_ref,
        asset_refs=body.asset_refs,
        rights_evidence_refs=body.rights_evidence_refs,
        rebuild_recipe=body.rebuild_recipe,
        principal=principal,
        manifest_id=body.manifest_id,
    )
    await uow.commit()
    return manifest.to_dict()
