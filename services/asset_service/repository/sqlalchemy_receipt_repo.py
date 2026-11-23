"""登记回执仓储（append-only，不提供 update/delete）。

回执的业务幂等键是 ``(tenant_id, generation_task_id)``，不是随机 entity_id。
并发下两个 relay 副本可能同时判空后插入，唯一约束冲突必须被收敛为「回读既有
回执」，不能把重复投递推成死信。
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from packages.shared.port_errors import translate_write_error
from services.asset_service.domain.asset_registration_receipt import (
    AssetRegistrationReceipt,
)
from services.asset_service.domain.enums import AssetRegistrationReceiptStatus
from services.asset_service.repository.orm import AssetRegistrationReceiptModel


def _to_orm(receipt: AssetRegistrationReceipt) -> AssetRegistrationReceiptModel:
    return AssetRegistrationReceiptModel(
        entity_id=receipt.entity_id,
        entity_type=receipt.ENTITY_TYPE,
        schema_version=receipt.schema_version,
        entity_version=receipt.entity_version,
        status=receipt.status.value,
        owner_module=receipt.owner_module,
        tenant_id=receipt.tenant_id,
        created_at=receipt.created_at,
        created_by=receipt.created_by,
        updated_at=receipt.updated_at,
        audit_ref=receipt.audit_ref,
        generation_task_id=receipt.generation_task_id,
        project_id=receipt.project_id,
        asset_version_id=receipt.asset_version_id or None,
        message=receipt.message or None,
    )


def _to_domain(model: AssetRegistrationReceiptModel) -> AssetRegistrationReceipt:
    return AssetRegistrationReceipt(
        entity_id=model.entity_id,
        created_by=model.created_by,
        tenant_id=model.tenant_id,
        schema_version=model.schema_version,
        entity_version=model.entity_version,
        status=AssetRegistrationReceiptStatus(model.status),
        owner_module=model.owner_module,
        created_at=model.created_at,
        updated_at=model.updated_at,
        audit_ref=model.audit_ref,
        generation_task_id=model.generation_task_id,
        project_id=model.project_id,
        asset_version_id=model.asset_version_id or "",
        message=model.message or "",
    )


class SqlAlchemyAssetRegistrationReceiptRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_tenant_task(
        self, tenant_id: UUID, generation_task_id: str
    ) -> AssetRegistrationReceipt | None:
        result = await self._session.execute(
            select(AssetRegistrationReceiptModel).where(
                AssetRegistrationReceiptModel.tenant_id == tenant_id,
                AssetRegistrationReceiptModel.generation_task_id == generation_task_id,
            )
        )
        model = result.scalar_one_or_none()
        return _to_domain(model) if model is not None else None

    async def add(self, receipt: AssetRegistrationReceipt) -> None:
        # SAVEPOINT 让唯一冲突后 session 仍可用，调用方能回读既有回执。
        try:
            async with self._session.begin_nested():
                self._session.add(_to_orm(receipt))
        except (IntegrityError, ProgrammingError) as exc:
            translate_write_error(exc, entity_id=receipt.entity_id)


__all__ = ["SqlAlchemyAssetRegistrationReceiptRepository"]
