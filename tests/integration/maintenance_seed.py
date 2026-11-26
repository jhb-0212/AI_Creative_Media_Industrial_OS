"""维护工单的测试夹具：一张工单要「合法出生 + 显式批准」才算可用。

单独成模块是因为两个测试文件都要造工单，而复制一份 INSERT 语句就会出现
「一处加了新必填列、另一处没加」的漂移——测出来的通过也就不可信了。
"""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

__all__ = [
    "WORK_ORDER_INSERT_SQL",
    "approve_work_order",
    "create_work_order",
]

WORK_ORDER_INSERT_SQL = """
INSERT INTO qc_maintenance.maintenance_work_orders
    (entity_id, entity_type, schema_version, entity_version, status,
     owner_module, tenant_id, created_at, created_by, updated_at, audit_ref,
     kind, idempotency_key, requested_by, approved_by, approved_at,
     target_task_id, result_summary, failure_reason)
VALUES (:entity_id,'MaintenanceWorkOrder',1,1,'REQUESTED','qc_maintenance',
        :tenant_id, now(),'tester',now(),NULL,
        :kind, :idem, 'tester', NULL, NULL, NULL, NULL, NULL)
"""

_APPROVE_SQL = """
UPDATE qc_maintenance.maintenance_work_orders
   SET status = 'APPROVED', approved_by = 'approver', approved_at = now(),
       entity_version = entity_version + 1, updated_at = now()
 WHERE entity_id = :entity_id
"""


async def approve_work_order(session: AsyncSession, entity_id: str) -> None:
    """REQUESTED → APPROVED：审批链必须显式走这一步（触发器不允许直接出生为已批）。"""
    await session.execute(text(_APPROVE_SQL), {"entity_id": entity_id})


async def create_work_order(
    session: AsyncSession,
    *,
    tenant,
    kind: str = "RECONCILE",
    idem: str | None = None,
    approve: bool = True,
) -> str:
    entity_id = str(uuid.uuid4())
    await session.execute(
        text(WORK_ORDER_INSERT_SQL),
        {
            "entity_id": entity_id,
            "tenant_id": tenant,
            "kind": kind,
            "idem": idem or f"idem-{entity_id}",
        },
    )
    if approve:
        await approve_work_order(session, entity_id)
    await session.commit()
    return entity_id
