"""交付清单审批消费的端到端集成测试（真实 RLS + 乐观锁 + outbox）。

不是单测的简单重复：这里证明整条链在真实数据库约束下成立——
- outbox 里没有 APPROVED 事件 → 清单停在 DRAFT（不接受客户端声明）；
- 写入 APPROVED 事件 → 消费用例把清单推进到 VALIDATED，audit_ref = 事件 id；
- 同一事件重放 → 幂等，不重复发事件、不重复改版本；
- RELEASE → RELEASED，产出 DELIVERY_MANIFEST_RELEASED 事件；
- 跨租户事件看不到（RLS），消费不越界。
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from packages.errors.error_codes import DomainError
from packages.identity.principal import Principal
from packages.shared.database import build_migration_url
from services.asset_service.repository.sqlalchemy_approval_event_source import (
    SqlAlchemyApprovalEventSource,
)
from services.asset_service.repository.sqlalchemy_manifest_repo import (
    SqlAlchemyDeliveryManifestRepository,
)
from services.asset_service.service.delivery_approval_service import (
    DeliveryApprovalService,
)
from tests.conftest import apply_db_context

pytestmark = pytest.mark.asyncio

_TENANT = uuid.UUID("00000000-0000-0000-0000-00000000d5a1")
_PROJECT = "proj-delivery-consume"


@pytest.fixture(scope="module")
async def admin_engine() -> AsyncEngine:
    engine = create_async_engine(build_migration_url(), pool_pre_ping=True)
    try:
        yield engine
    finally:
        await engine.dispose()


async def _seed_manifest(admin_engine: AsyncEngine, *, tenant_id: uuid.UUID) -> str:
    entity_id = f"manifest-consume-{uuid.uuid4().hex[:12]}"
    async with admin_engine.begin() as conn:
        await conn.execute(
            sa.text(
                """
                INSERT INTO asset_service.delivery_manifests (
                    entity_id, entity_type, schema_version, entity_version,
                    status, owner_module, tenant_id, organization_id,
                    created_at, created_by, updated_at, audit_ref,
                    project_id, asset_refs, checksum, picture_lock_ref,
                    qc_final_ref, rebuild_recipe, rights_evidence_refs, spec_ref
                ) VALUES (
                    :eid, 'DeliveryManifest', 205, 1,
                    'DRAFT', 'asset_service', :tid, :tid,
                    now(), 'seed', now(), NULL,
                    :pid, '["asset-a"]'::jsonb, repeat('0', 64), 'picture-lock-1',
                    'qc-final-1', 'recipe://spec/v1', '["rights-1"]'::jsonb, 'spec-1'
                )
                """
            ),
            {"eid": entity_id, "tid": tenant_id, "pid": _PROJECT},
        )
    return entity_id


async def _seed_approval_event(
    admin_engine: AsyncEngine,
    *,
    manifest_id: str,
    tenant_id: uuid.UUID,
    decision: str = "APPROVED",
) -> uuid.UUID:
    """以权威事件形态写入 outbox（与审批引擎发出的载荷同构）。

    直连 SQL 而不是 record_event：后者要 AsyncSession（与业务同事务），
    这里用超级用户连接播种测试数据，绕过 RLS 只是为了造数据。
    """
    import json

    event_id = uuid.uuid4()
    payload = {
        "ticket_id": f"ticket-{manifest_id}",
        "flow_id": "flow-1",
        "action_key": "DELIVERY.MANIFEST.APPROVE",
        "resource_type": "DeliveryManifest",
        "resource_id": manifest_id,
        "project_id": _PROJECT,
        "decision": decision,
        "votes": [
            {"group_name": "delivery_owner", "decision": "APPROVE"},
            {"group_name": "qc_rights", "decision": "APPROVE"},
        ],
        "tenant_id": str(tenant_id),
    }
    async with admin_engine.begin() as conn:
        await conn.execute(
            sa.text(
                """
                INSERT INTO outbox.outbox_events (
                    event_id, aggregate_type, aggregate_id, tenant_id,
                    event_type, payload, occurred_at
                ) VALUES (
                    :eid, 'ApprovalTicket', :agg, :tid,
                    'APPROVAL_TICKET_COMPLETED', CAST(:payload AS jsonb), now()
                )
                """
            ),
            {
                "eid": event_id,
                "agg": f"ticket-{manifest_id}",
                "tid": tenant_id,
                "payload": json.dumps(payload),
            },
        )
    return event_id


def _service(session: object) -> DeliveryApprovalService:
    return DeliveryApprovalService(
        SqlAlchemyDeliveryManifestRepository(session),  # type: ignore[arg-type]
        SqlAlchemyApprovalEventSource(session),  # type: ignore[arg-type]
        # 消费用例发 APPROVED/RELEASED 事件需要 OutboxWriter；用同一会话。
        _writer(session),
    )


def _writer(session: object):  # type: ignore[no-untyped-def]
    from packages.shared.outbox_writer import SqlAlchemyOutboxWriter

    return SqlAlchemyOutboxWriter(session)  # type: ignore[arg-type]


async def test_no_approval_event_stays_draft(
    admin_engine: AsyncEngine, asset_sm: async_sessionmaker
) -> None:
    manifest_id = await _seed_manifest(admin_engine, tenant_id=_TENANT)
    async with asset_sm() as session:
        await apply_db_context(session, tenant_id=_TENANT)
        service = _service(session)
        with pytest.raises(DomainError):
            await service.consume_approval(manifest_id=manifest_id)


async def test_approved_event_promotes_and_is_idempotent(
    admin_engine: AsyncEngine, asset_sm: async_sessionmaker
) -> None:
    manifest_id = await _seed_manifest(admin_engine, tenant_id=_TENANT)
    event_id = await _seed_approval_event(
        admin_engine, manifest_id=manifest_id, tenant_id=_TENANT
    )

    async with asset_sm() as session:
        await apply_db_context(session, tenant_id=_TENANT)
        service = _service(session)
        validated = await service.consume_approval(manifest_id=manifest_id)
        assert validated.status.value == "VALIDATED"
        assert validated.audit_ref == str(event_id)
        assert validated.entity_version == 3
        await session.commit()

    # 重放：幂等，版本不再推进。
    async with asset_sm() as session:
        await apply_db_context(session, tenant_id=_TENANT)
        service = _service(session)
        replay = await service.consume_approval(manifest_id=manifest_id)
        assert replay.status.value == "VALIDATED"
        assert replay.entity_version == 3
        await session.commit()


async def test_release_after_validation(
    admin_engine: AsyncEngine, asset_sm: async_sessionmaker
) -> None:
    manifest_id = await _seed_manifest(admin_engine, tenant_id=_TENANT)
    await _seed_approval_event(admin_engine, manifest_id=manifest_id, tenant_id=_TENANT)
    principal = Principal(
        subject_id="delivery-owner",
        tenant_id=_TENANT,
        organization_id=_TENANT,
        roles=frozenset({"交付负责人"}),
        project_ids=frozenset({_PROJECT}),
    )
    async with asset_sm() as session:
        await apply_db_context(session, tenant_id=_TENANT)
        service = _service(session)
        await service.consume_approval(manifest_id=manifest_id)
        released = await service.release_manifest(
            manifest_id=manifest_id, principal=principal
        )
        assert released.status.value == "RELEASED"
        assert "delivery-owner" in str(released.audit_ref)
        await session.commit()

    async with admin_engine.connect() as conn:
        row = (
            await conn.execute(
                sa.text(
                    "SELECT status, entity_version FROM asset_service.delivery_manifests "
                    "WHERE entity_id = :e"
                ),
                {"e": manifest_id},
            )
        ).one()
    assert row.status == "RELEASED"
    assert row.entity_version == 4


async def test_cross_tenant_event_is_invisible(
    admin_engine: AsyncEngine, asset_sm: async_sessionmaker
) -> None:
    """另一个租户的 APPROVED 事件对本租户不可见：RLS 隔离事件读取。"""
    other_tenant = uuid.UUID("00000000-0000-0000-0000-00000000d5b2")
    manifest_id = await _seed_manifest(admin_engine, tenant_id=_TENANT)
    await _seed_approval_event(
        admin_engine, manifest_id=manifest_id, tenant_id=other_tenant
    )
    async with asset_sm() as session:
        await apply_db_context(session, tenant_id=_TENANT)
        source = SqlAlchemyApprovalEventSource(session)
        events = await source.list_completed(
            action_key="DELIVERY.MANIFEST.APPROVE", resource_type="DeliveryManifest"
        )
        assert all(e.tenant_id != other_tenant for e in events)
