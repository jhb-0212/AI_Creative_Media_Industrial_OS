"""Outbox 分区布局与 RLS 在真实库上的行为（R06 保留期）。

证明三件事：
1. 当前月份分区存在，且写入经父表落到对应月份分区（tableoid 证明）；
2. 父表 RLS 策略对分区生效：无租户上下文读不到任何行（不能因为换成分区就漏）；
3. app_role 仍然没有 DELETE（append-only 承诺在分区化后不变）。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from packages.shared.database import build_migration_url
from packages.shared.outbox_writer import record_event
from packages.shared.rls import set_tenant_context

TENANT = uuid.uuid4()


@pytest.fixture(scope="module")
async def admin_engine() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(build_migration_url())
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture(autouse=True)
async def clean_events(admin_engine: AsyncEngine) -> AsyncIterator[None]:
    yield
    async with admin_engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM outbox.outbox_events WHERE tenant_id = :t"),
            {"t": TENANT},
        )


async def test_current_month_partition_exists(admin_engine: AsyncEngine) -> None:
    """当月分区必须已存在：否则写入全部落到 DEFAULT，保留期无法按月份摘除。"""
    month = datetime.now(UTC).strftime("%Y%m")
    async with admin_engine.connect() as conn:
        exists = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM pg_class c "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = 'outbox' AND c.relname = :name"
                ),
                {"name": f"outbox_events_y{month}"},
            )
        ).scalar_one()
    assert exists == 1, f"缺少当月分区 outbox_events_y{month}，请先跑 workers.outbox_maintenance"


async def test_write_routes_to_month_partition(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """写入必须路由到 occurred_at 所在月份的分区，而不是 DEFAULT。"""
    async with db_sessionmaker() as session:
        await set_tenant_context(session, TENANT)
        event_id = record_event(
            session,
            aggregate_type="ScriptRevision",
            aggregate_id="agg-partition",
            tenant_id=TENANT,
            event_type="SCRIPT.IMPORTED",
            payload={"probe": "partition"},
        )
        await session.flush()
        # 在同一个事务内查 tableoid：SET LOCAL 的租户上下文只在本事务有效，
        # commit 后再查会被 RLS 过滤成 0 行。
        tableoid = (
            await session.execute(
                text(
                    "SELECT tableoid::regclass::text FROM outbox.outbox_events "
                    "WHERE event_id = :eid"
                ),
                {"eid": event_id},
            )
        ).scalar_one()
        await session.commit()
    assert tableoid.split(".")[-1].startswith("outbox_events_y"), tableoid


async def test_rls_still_blocks_without_tenant_context(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """分区化之后父表策略仍生效：无租户上下文读到 0 行。"""
    async with db_sessionmaker() as session:
        count = (
            await session.execute(text("SELECT count(*) FROM outbox.outbox_events"))
        ).scalar_one()
    assert count == 0


async def test_app_role_has_no_delete_privilege(admin_engine: AsyncEngine) -> None:
    """append-only 不因分区化而回退：app_role 对父表不得有 DELETE。"""
    async with admin_engine.connect() as conn:
        privileges = (
            await conn.execute(
                text(
                    "SELECT privilege_type FROM information_schema.role_table_grants "
                    "WHERE table_schema = 'outbox' AND table_name = 'outbox_events' "
                    "AND grantee = 'app_role'"
                )
            )
        ).scalars()
        granted = set(privileges)
    assert granted == {"SELECT", "INSERT", "UPDATE"}
