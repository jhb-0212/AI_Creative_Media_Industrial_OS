"""交付清单审批事件消费 worker（D4）。

为什么需要独立 worker 而不是塞进 outbox relay：
- relay 用 OUTBOX_RELAY 角色，只能读事件 + 回写投递状态列，**写不了业务表**
  （workers/relay/__main__.py 的安全边界）；
- 消费审批结论要写 delivery_manifests 的 status，必须用 ASSET 角色，
  因此独立成一个 worker，与 relay 共用 outbox 但各用各的数据库角色。

租户隔离：与 relay 一样逐租户处理。跨租户枚举能力
（`outbox.list_active_tenants()`）只授给 relay 角色；本 worker 不擅自扩大
资产域权限，而是由调用方显式给出 tenant_id 列表（运维/编排层决定消费范围）。

幂等：消费以清单状态为准，不是以"事件是否投递"为准。同一事件重放时清单已
VALIDATED 且 audit_ref 一致 → 直接返回；不同事件试图再推进终态 → 拒绝。
这与 outbox「至少一次」语义相容，无需 worker 侧维护已读表。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.errors.error_codes import DomainError
from packages.shared.logging import get_logger
from packages.shared.outbox_writer import SqlAlchemyOutboxWriter
from packages.shared.rls import set_tenant_context
from services.asset_service.repository.sqlalchemy_approval_event_source import (
    SqlAlchemyApprovalEventSource,
)
from services.asset_service.repository.sqlalchemy_manifest_repo import (
    SqlAlchemyDeliveryManifestRepository,
)
from services.asset_service.service.delivery_approval_service import (
    _APPROVE_ACTION_KEY,
    _APPROVE_RESOURCE_TYPE,
    DeliveryApprovalService,
)

logger = get_logger(__name__)

SessionMaker = async_sessionmaker[AsyncSession]


@dataclass
class DeliveryConsumeStats:
    """单轮消费统计。"""

    tenants_scanned: int = 0
    events_seen: int = 0
    promoted: int = 0
    skipped: int = 0
    failed: int = 0


async def consume_tenant(
    sessionmaker: SessionMaker,
    tenant_id: uuid.UUID,
    stats: DeliveryConsumeStats,
) -> None:
    """在单个租户上下文内消费一轮（该租户一个事务）。"""
    async with sessionmaker() as session:
        await set_tenant_context(session, tenant_id)
        source = SqlAlchemyApprovalEventSource(session)
        service = DeliveryApprovalService(
            SqlAlchemyDeliveryManifestRepository(session),
            source,
            SqlAlchemyOutboxWriter(session),
        )
        events = await source.list_completed(
            action_key=_APPROVE_ACTION_KEY,
            resource_type=_APPROVE_RESOURCE_TYPE,
        )
        for event in events:
            stats.events_seen += 1
            if event.decision != "APPROVED":
                stats.skipped += 1
                continue
            try:
                await service.consume_approval(manifest_id=event.resource_id)
            except DomainError:
                # 终态重复的推进会被领域层拒绝——这是幂等，不是失败。
                stats.skipped += 1
            else:
                stats.promoted += 1
        await session.commit()


async def run_once(
    sessionmaker: SessionMaker, *, tenant_ids: tuple[uuid.UUID, ...]
) -> DeliveryConsumeStats:
    """扫描给定租户，消费所有已通过的交付审批事件。"""
    stats = DeliveryConsumeStats()
    for tenant_id in tenant_ids:
        stats.tenants_scanned += 1
        try:
            await consume_tenant(sessionmaker, tenant_id, stats)
        except Exception:
            # 单租户失败不影响其它租户；不吞掉堆栈（可观测性）。
            stats.failed += 1
            logger.exception("delivery_consumer_tenant_failed", tenant_id=str(tenant_id))
    return stats


__all__ = ["DeliveryConsumeStats", "consume_tenant", "run_once"]
