"""资产域 API 路由（薄层，只做协议适配与 DTO 转换）。

契约对齐（contracts/action_registry.yaml）：
- ASSET.VERSION.CREATE  → POST /assets
- QC.REPORT.CREATE      → POST /assets/{asset_version_id}/qc-reports
- ASSET.VERSION.APPROVE → POST /assets/{asset_version_id}/approve
- ASSET.VERSION.READ    → GET  /assets/{asset_version_id}

身份与权限：
- created_by / approved_by 均来自可信 Principal（JWT），不接受客户端字符串。
- 四个动作各自经 require_action 做运行时授权决策（ActionSpec 来自契约）。

读能力边界（ADR-R08 G2 已关闭）：
- READ 只返回版本信封与引用地址，不签发存储凭证；租户外资产由 RLS 隔离，
  统一回落 NOT_FOUND，避免用 403/404 差异探测他人租户资产。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, create_model
from sqlalchemy.ext.asyncio import AsyncSession

from packages.identity.authorization import ResourceRef
from packages.identity.enforcement import authorize
from packages.shared.contract_models import contract_model
from packages.shared.outbox_writer import SqlAlchemyOutboxWriter
from packages.shared.unit_of_work import UnitOfWork
from services.asset_service.domain.enums import DeviationClass, IntentMatchStatus
from services.asset_service.repository.sqlalchemy_asset_repo import (
    SqlAlchemyAssetRepository,
)
from services.asset_service.repository.sqlalchemy_qc_repo import (
    SqlAlchemyQCReportRepository,
)
from services.asset_service.service.asset_service import AssetService
from services.identity_access.deps import (
    Principal,
    get_asset_uow,
    get_read_asset_uow,
    require_action,
)

router = APIRouter(prefix="/assets", tags=["asset-management"])

# 响应模型直接由契约定义生成：字段集永远等于 object_schemas，多出来的内部
# 字段（如 tenant_id）会被响应模型过滤掉（P2-4）。
_AssetVersionResponse = contract_model("AssetVersion")
_QCReportResponse = contract_model("QCReport")
_QCCreationResponse = create_model(
    "QCReportCreation",
    asset_version=(_AssetVersionResponse, ...),
    qc_report=(_QCReportResponse, ...),
)


class CreateAssetVersionRequest(BaseModel):
    """资产版本登记请求体（ASSET.VERSION.CREATE）。

    ``project_id`` 是归属必填项：没有它资产无法被项目级授权覆盖，
    也无从追溯来源（审查第三轮 F2）。
    """

    project_id: str = Field(..., min_length=1, max_length=64)
    asset_type: str = Field(..., min_length=1, max_length=32)
    sha256: str = Field(..., min_length=64, max_length=64)
    storage_ref: dict[str, Any] = Field(..., min_length=1)
    source_asset_version_ids: list[str] = Field(default_factory=list)
    rights_manifest_ref: str | None = Field(default=None, max_length=64)
    adapter_id: str | None = Field(default=None, max_length=64)
    tool_product: str | None = Field(default=None, max_length=64)
    operator_ref: str | None = Field(default=None, max_length=64)
    version: str | None = Field(default=None, max_length=32)
    entity_id: str | None = Field(default=None, max_length=64)


class CreateQCReportRequest(BaseModel):
    """QC 证据登记请求体（QC.REPORT.CREATE）。"""

    quality_profile_version: str = Field(..., min_length=1, max_length=64)
    detector_versions: dict[str, Any] = Field(..., min_length=1)
    findings: list[Any] = Field(..., min_length=1)
    benchmark_version: str | None = Field(default=None, max_length=64)
    deviation_class: DeviationClass | None = None
    deviation_intent_ref: str | None = Field(default=None, max_length=64)
    intent_match_status: IntentMatchStatus | None = None
    human_decision: str | None = Field(default=None, max_length=32)
    qc_report_id: str | None = Field(default=None, max_length=64)


def _build_service(session: AsyncSession) -> AssetService:
    """服务装配：RLS 会话 + 两个仓储端口 + Outbox 写入端口。"""
    return AssetService(
        SqlAlchemyAssetRepository(session),
        SqlAlchemyQCReportRepository(session),
        SqlAlchemyOutboxWriter(session),
    )


def _get_service(uow: UnitOfWork = Depends(get_asset_uow)) -> AssetService:
    """写用例服务装配（提交点由路由显式控制）。"""
    return _build_service(uow.session)


def _get_read_service(
    uow: UnitOfWork = Depends(get_read_asset_uow),
) -> AssetService:
    """只读用例装配：走只读 UoW，不占用写事务（P2-3）。"""
    return _build_service(uow.session)


@router.post("", status_code=201, response_model=_AssetVersionResponse)
async def create_asset_version(
    body: CreateAssetVersionRequest,
    principal: Principal = Depends(require_action("ASSET.VERSION.CREATE")),
    uow: UnitOfWork = Depends(get_asset_uow),
    service: AssetService = Depends(_get_service),
) -> dict[str, Any]:
    """登记资产版本（GENERATED，幂等；禁止覆盖既有版本内容）。"""
    # 资源级授权（B2）：组织取调用主体的组织（归属由服务端写入，不接受客户端声明），
    # 项目取请求体 project_id —— 越界项目在依赖阶段查不到，必须在这里真判一次。
    authorize(
        principal,
        "ASSET.VERSION.CREATE",
        resource=ResourceRef(
            resource_type="AssetVersion",
            organization_id=principal.organization_id,
            project_id=body.project_id,
        ),
    )
    asset = await service.create_version(
        project_id=body.project_id,
        asset_type=body.asset_type,
        sha256=body.sha256,
        storage_ref=body.storage_ref,
        source_asset_version_ids=body.source_asset_version_ids,
        rights_manifest_ref=body.rights_manifest_ref,
        adapter_id=body.adapter_id,
        tool_product=body.tool_product,
        operator_ref=body.operator_ref,
        version=body.version,
        principal=principal,
        entity_id=body.entity_id,
    )
    await uow.commit()
    return asset.to_dict()


@router.post("/{asset_version_id}/qc-reports", status_code=201, response_model=_QCCreationResponse)
async def create_qc_report(
    asset_version_id: str,
    body: CreateQCReportRequest,
    principal: Principal = Depends(require_action("QC.REPORT.CREATE")),
    uow: UnitOfWork = Depends(get_asset_uow),
    service: AssetService = Depends(_get_service),
) -> dict[str, Any]:
    """登记 QC 证据（首个报告把资产推入 TECHNICAL_QC，不改变 Gate）。"""
    asset, report = await service.create_qc_report(
        asset_version_id=asset_version_id,
        quality_profile_version=body.quality_profile_version,
        detector_versions=body.detector_versions,
        findings=body.findings,
        principal=principal,
        benchmark_version=body.benchmark_version,
        deviation_class=body.deviation_class,
        deviation_intent_ref=body.deviation_intent_ref,
        intent_match_status=body.intent_match_status,
        human_decision=body.human_decision,
        qc_report_id=body.qc_report_id,
    )
    await uow.commit()
    return {"asset_version": asset.to_dict(), "qc_report": report.to_dict()}


@router.post("/{asset_version_id}/approve", response_model=_AssetVersionResponse)
async def approve_asset_version(
    asset_version_id: str,
    principal: Principal = Depends(require_action("ASSET.VERSION.APPROVE")),
    uow: UnitOfWork = Depends(get_asset_uow),
    service: AssetService = Depends(_get_service),
) -> dict[str, Any]:
    """批准资产版本（职责分离 + 权利/QC 证据齐备；TECHNICAL_QC 先过 HUMAN_REVIEW）。"""
    asset = await service.approve(
        asset_version_id=asset_version_id, principal=principal
    )
    await uow.commit()
    return asset.to_dict()


@router.get("/{asset_version_id}", response_model=_AssetVersionResponse)
async def read_asset_version(
    asset_version_id: str,
    _principal: Principal = Depends(require_action("ASSET.VERSION.READ")),
    service: AssetService = Depends(_get_read_service),
) -> dict[str, Any]:
    """读取资产版本信封（只读；租户可见性由 RLS 保证，不可见即 NOT_FOUND）。"""
    asset = await service.get_version(asset_version_id=asset_version_id)
    return asset.to_dict()
