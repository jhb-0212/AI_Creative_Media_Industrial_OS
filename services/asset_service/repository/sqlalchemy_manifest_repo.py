"""DeliveryManifest 的 SQLAlchemy 仓储实现（asset_service 模块专属写入口）。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.shared.persistence import translate_integrity_errors
from services.asset_service.domain.delivery_manifest import DeliveryManifest
from services.asset_service.domain.enums import DeliveryManifestStatus
from services.asset_service.repository.manifest_repo import (
    DeliveryManifestRepository,
)
from services.asset_service.repository.orm import DeliveryManifestModel


def _to_domain(model: DeliveryManifestModel) -> DeliveryManifest:
    """ORM -> 领域模型（存量非法状态转结构化错误，不裸抛 ValueError）。"""
    try:
        status = DeliveryManifestStatus(model.status)
    except ValueError as exc:
        raise DomainError(
            ErrorRegistry.INTERNAL_ERROR,
            detail=f"delivery_manifests 存量状态非法: {model.status!r}",
        ) from exc
    return DeliveryManifest(
        manifest_id=model.entity_id,
        project_id=model.project_id,
        asset_refs=list(model.asset_refs),
        spec_ref=model.spec_ref,
        qc_final_ref=model.qc_final_ref,
        picture_lock_ref=model.picture_lock_ref,
        rights_evidence_refs=list(model.rights_evidence_refs),
        rebuild_recipe=model.rebuild_recipe,
        created_by=model.created_by,
        tenant_id=model.tenant_id,
        organization_id=model.organization_id,
        status=status,
        schema_version=model.schema_version,
        entity_version=model.entity_version,
        owner_module=model.owner_module,
        created_at=model.created_at,
        updated_at=model.updated_at,
        audit_ref=model.audit_ref,
    )


def _to_orm(manifest: DeliveryManifest) -> DeliveryManifestModel:
    """领域模型 -> ORM。"""
    return DeliveryManifestModel(
        entity_id=manifest.manifest_id,
        entity_type="DeliveryManifest",
        schema_version=manifest.schema_version,
        entity_version=manifest.entity_version,
        status=manifest.status.value,
        owner_module=manifest.owner_module,
        tenant_id=manifest.tenant_id,
        organization_id=manifest.organization_id,
        created_at=manifest.created_at,
        created_by=manifest.created_by,
        updated_at=manifest.updated_at,
        audit_ref=manifest.audit_ref,
        project_id=manifest.project_id,
        asset_refs=manifest.asset_refs,
        checksum=manifest.checksum,
        picture_lock_ref=manifest.picture_lock_ref,
        qc_final_ref=manifest.qc_final_ref,
        rebuild_recipe=manifest.rebuild_recipe,
        rights_evidence_refs=manifest.rights_evidence_refs,
        spec_ref=manifest.spec_ref,
    )


class SqlAlchemyDeliveryManifestRepository(DeliveryManifestRepository):
    """基于 SQLAlchemy 异步会话的清单仓储（只插入，不更新）。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, manifest: DeliveryManifest) -> None:
        # SAVEPOINT：主键冲突只回滚本次 INSERT，session 不进入 aborted 态，
        # 调用方仍可回查既有清单做幂等判断。驱动异常在此翻译成领域异常。
        with translate_integrity_errors(entity=f"manifest_id={manifest.manifest_id}"):
            async with self._session.begin_nested():
                self._session.add(_to_orm(manifest))

    async def get_by_id(self, manifest_id: str) -> DeliveryManifest | None:
        stmt = select(DeliveryManifestModel).where(
            DeliveryManifestModel.entity_id == manifest_id
        )
        result = await self._session.execute(stmt)
        model = result.scalar_one_or_none()
        return _to_domain(model) if model is not None else None


# 供 contract_check 的序列化探针使用：构造最小合法对象。
PROBE_FIELDS: dict[str, Any] = {
    "manifest_id": "probe-manifest",
    "spec_ref": "spec-probe",
    "qc_final_ref": "qc-final-probe",
    "picture_lock_ref": "picture-lock-probe",
    "asset_refs": ["probe-asset"],
    "rights_evidence_refs": ["rights-probe"],
    "rebuild_recipe": "recipe://probe/v1",
    "created_by": "probe",
}


__all__ = ["SqlAlchemyDeliveryManifestRepository"]
