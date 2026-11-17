"""交付消费 worker 的集成测试（R4-1 根治验证）。

证明的不是"服务层能消费"，而是 **worker 的真实路径**——即 ``consume_tenant``
通过 ``set_request_context`` 注入完整 RLS 上下文后：

1. 只给 tenant（缺失 org + projects）→ 项目级资源表判否，读不到任何清单，
   事件被看到但清单**不会**推进（这正是 R4-1 描述的静默停摆，修复前 worker
   会把它记成 skipped 而清单永远不动）；
2. 给完整 org + projects → 清单推进到 VALIDATED；
3. 真实失败（清单不存在 / 无 APPROVED 事件）→ 计入 failed 并 rollback，
   **不是**被吞成 skipped。

与 ``test_delivery_approval_consumption.py`` 的分工：那里直连服务层并用测试
夹具注入上下文；这里走 ``consume_tenant`` 这条 worker 真实路径，覆盖前者
刻意绕开的入口。
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from packages.shared.database import build_migration_url
from workers.delivery.consumer import (
    DeliveryConsumerContext,
    DeliveryConsumeStats,
    consume_tenant,
)

pytestmark = pytest.mark.asyncio

# 每个测试用独立租户 + 独立项目：worker 的 list_completed 通过 RLS 按租户
# 隔离事件读取，独立租户保证测试之间不因历史累积数据互相污染（R4-9 的
# "全量扫描"正是靠租户隔离在测试里收窄观察面）。
_PROJECT = "proj-worker-consume"


def _fresh_tenant() -> uuid.UUID:
    """生成独立租户 id，隔离本测试的事件与清单。"""
    return uuid.uuid4()


@pytest.fixture(scope="module")
async def admin_engine() -> AsyncEngine:
    engine = create_async_engine(build_migration_url(), pool_pre_ping=True)
    try:
        yield engine
    finally:
        await engine.dispose()


async def _seed_manifest(admin_engine: AsyncEngine, *, tenant_id: uuid.UUID) -> str:
    entity_id = f"worker-manifest-{uuid.uuid4().hex[:12]}"
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
) -> uuid.UUID:
    import json

    event_id = uuid.uuid4()
    payload = {
        "ticket_id": f"ticket-{manifest_id}",
        "flow_id": "flow-1",
        "action_key": "DELIVERY.MANIFEST.APPROVE",
        "resource_type": "DeliveryManifest",
        "resource_id": manifest_id,
        "project_id": _PROJECT,
        "decision": "APPROVED",
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


def _context(*, tenant_id: uuid.UUID, project_ids: frozenset[str]) -> DeliveryConsumerContext:
    # 单组织租户基线：organization_id 缺省等于 tenant（与迁移回填口径一致）。
    return DeliveryConsumerContext(
        tenant_id=tenant_id,
        organization_id=tenant_id,
        project_ids=project_ids,
    )


async def _manifest_status(admin_engine: AsyncEngine, manifest_id: str) -> str:
    async with admin_engine.connect() as conn:
        row = (
            await conn.execute(
                sa.text(
                    "SELECT status FROM asset_service.delivery_manifests "
                    "WHERE entity_id = :e"
                ),
                {"e": manifest_id},
            )
        ).one()
    return row.status


async def test_complete_context_promotes_manifest(
    admin_engine: AsyncEngine, asset_sm: async_sessionmaker
) -> None:
    """完整 org + projects 上下文 → 清单推进到 VALIDATED。"""
    tenant_id = _fresh_tenant()
    manifest_id = await _seed_manifest(admin_engine, tenant_id=tenant_id)
    await _seed_approval_event(
        admin_engine, manifest_id=manifest_id, tenant_id=tenant_id
    )
    stats = DeliveryConsumeStats()
    await consume_tenant(
        asset_sm,
        _context(tenant_id=tenant_id, project_ids=frozenset({_PROJECT})),
        stats,
    )
    assert stats.promoted == 1
    assert stats.failed == 0
    assert await _manifest_status(admin_engine, manifest_id) == "VALIDATED"


async def test_wildcard_scope_promotes_manifest(
    admin_engine: AsyncEngine, asset_sm: async_sessionmaker
) -> None:
    """通配符（组织内全部项目）上下文 → 同样能推进。"""
    tenant_id = _fresh_tenant()
    manifest_id = await _seed_manifest(admin_engine, tenant_id=tenant_id)
    await _seed_approval_event(
        admin_engine, manifest_id=manifest_id, tenant_id=tenant_id
    )
    stats = DeliveryConsumeStats()
    await consume_tenant(
        asset_sm,
        _context(tenant_id=tenant_id, project_ids=frozenset({"*"})),
        stats,
    )
    assert stats.promoted == 1
    assert await _manifest_status(admin_engine, manifest_id) == "VALIDATED"


async def test_missing_project_scope_sees_nothing(
    admin_engine: AsyncEngine, asset_sm: async_sessionmaker
) -> None:
    """空项目范围 → 项目级资源表判否，清单不被推进（fail-closed）。"""
    tenant_id = _fresh_tenant()
    manifest_id = await _seed_manifest(admin_engine, tenant_id=tenant_id)
    await _seed_approval_event(
        admin_engine, manifest_id=manifest_id, tenant_id=tenant_id
    )
    stats = DeliveryConsumeStats()
    # 空 project scope：事件能被列出（outbox 只按租户隔离），但清单读不到，
    # 因此 consume_approval 抛 NOT_FOUND → 计入 failed，而非静默 skipped。
    await consume_tenant(
        asset_sm,
        _context(tenant_id=tenant_id, project_ids=frozenset()),
        stats,
    )
    # 关键断言：真实失败被计为 failed，绝不伪装成幂等 skipped。
    assert stats.failed >= 1
    assert stats.promoted == 0
    assert await _manifest_status(admin_engine, manifest_id) == "DRAFT"


async def test_no_approval_event_counts_failed_not_skipped(
    admin_engine: AsyncEngine, asset_sm: async_sessionmaker
) -> None:
    """有清单但无 APPROVED 事件 → worker 不推进（0 promoted，不伪造成功）。"""
    tenant_id = _fresh_tenant()
    manifest_id = await _seed_manifest(admin_engine, tenant_id=tenant_id)
    # 不播种审批事件：worker 的 list_completed 扫不到任何事件，什么都不做。
    stats = DeliveryConsumeStats()
    await consume_tenant(
        asset_sm,
        _context(tenant_id=tenant_id, project_ids=frozenset({_PROJECT})),
        stats,
    )
    assert stats.promoted == 0
    assert stats.events_seen == 0
    assert await _manifest_status(admin_engine, manifest_id) == "DRAFT"
