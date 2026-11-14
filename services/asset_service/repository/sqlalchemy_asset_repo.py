"""AssetVersion 的 SQLAlchemy 仓储实现（数据所有权：asset_service 模块）。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.shared.versioning import _orm_update_values, conditional_update
from services.asset_service.domain.asset_version import AssetVersion
from services.asset_service.domain.enums import AssetStatus
from services.asset_service.repository.asset_repo import AssetRepository
from services.asset_service.repository.orm import AssetVersionModel


def _to_domain(model: AssetVersionModel) -> AssetVersion:
    """ORM -> 领域模型（非法存量状态转结构化错误，不裸抛 ValueError）。"""
    try:
        status = AssetStatus(model.status)
    except ValueError as exc:
        raise DomainError(
            ErrorRegistry.INTERNAL_ERROR,
            detail=f"asset_versions 存量状态非法: {model.status!r}",
        ) from exc
    return AssetVersion(
        entity_id=model.entity_id,
        created_by=model.created_by,
        tenant_id=model.tenant_id,
        status=status,
        schema_version=model.schema_version,
        entity_version=model.entity_version,
        owner_module=model.owner_module,
        created_at=model.created_at,
        updated_at=model.updated_at,
        audit_ref=model.audit_ref,
        asset_type=model.asset_type,
        sha256=model.sha256,
        storage_ref=dict(model.storage_ref),
        source_asset_version_ids=list(model.source_asset_version_ids),
        rights_manifest_ref=model.rights_manifest_ref,
        adapter_id=model.adapter_id,
        tool_product=model.tool_product,
        operator_ref=model.operator_ref,
        version=model.version,
    )


def _to_orm(asset: AssetVersion) -> AssetVersionModel:
    """领域模型 -> ORM。"""
    return AssetVersionModel(
        entity_id=asset.entity_id,
        entity_type=asset.ENTITY_TYPE,
        schema_version=asset.schema_version,
        entity_version=asset.entity_version,
        status=asset.status.value,
        owner_module=asset.owner_module,
        tenant_id=asset.tenant_id,
        created_at=asset.created_at,
        created_by=asset.created_by,
        updated_at=asset.updated_at,
        audit_ref=asset.audit_ref,
        asset_type=asset.asset_type,
        sha256=asset.sha256,
        storage_ref=asset.storage_ref,
        source_asset_version_ids=asset.source_asset_version_ids,
        rights_manifest_ref=asset.rights_manifest_ref,
        adapter_id=asset.adapter_id,
        tool_product=asset.tool_product,
        operator_ref=asset.operator_ref,
        version=asset.version,
    )


class SqlAlchemyAssetRepository(AssetRepository):
    """基于 SQLAlchemy 异步会话的仓储实现。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, asset: AssetVersion) -> None:
        # SAVEPOINT：主键冲突只回滚本次 INSERT，session 不进入 aborted 态，
        # 调用方仍可查询既有记录做幂等判断。
        async with self._session.begin_nested():
            self._session.add(_to_orm(asset))

    async def save(self, asset: AssetVersion, expected_version: int) -> None:
        await conditional_update(
            self._session,
            AssetVersionModel,
            asset.entity_id,
            expected_version,
            _orm_update_values(_to_orm(asset)),
        )

    async def get_by_id(self, entity_id: str) -> AssetVersion | None:
        stmt = select(AssetVersionModel).where(AssetVersionModel.entity_id == entity_id)
        result = await self._session.execute(stmt)
        model = result.scalar_one_or_none()
        return _to_domain(model) if model is not None else None
