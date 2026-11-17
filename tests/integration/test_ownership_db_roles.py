"""数据库角色边界负向测试（F03 数据所有权：进程边界隔离）。

验证（需真实 PostgreSQL，迁移 7a8b9c0d1e2f 已应用）：
1. outbox_relay_role 能 SELECT / UPDATE outbox.outbox_events（领取 + 回写投递状态）。
2. outbox_relay_role 不能 INSERT / DELETE outbox.outbox_events（事件只能由
   业务域同事务写入，relay 不得伪造/删除审计证据）。
3. outbox_relay_role 没有 project_planning / asset_service 等业务 schema 的
   权限（越界写业务表被数据库层拒绝，而非依赖代码自觉）。

这是 F03「数据所有权下沉到数据库权限边界」的负向证明：即使 Relay 代码
被攻破或误改，它也无法直接写业务数据。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from packages.shared.database import build_relay_database_url

_RELAY_ENGINE: AsyncEngine | None = None


@pytest.fixture(scope="module")
async def relay_engine() -> AsyncIterator[AsyncEngine]:
    """outbox_relay_role 连接（Relay 运行时的真实连接角色）。"""
    engine = create_async_engine(build_relay_database_url())
    try:
        yield engine
    finally:
        await engine.dispose()


async def test_relay_can_select_outbox(relay_engine: AsyncEngine) -> None:
    """relay 能读 outbox_events（领取待投递事件的前提）。"""
    async with relay_engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT count(*) FROM outbox.outbox_events WHERE false")
        )
        assert rows.scalar() == 0


async def test_relay_can_update_published_at(relay_engine: AsyncEngine) -> None:
    """relay 能 UPDATE outbox_events（回写 published_at 投递状态）。"""
    async with relay_engine.begin() as conn:
        # 空更新：WHERE false 命中 0 行，仅验证 UPDATE 权限存在、不产生副作用。
        result = await conn.execute(
            text(
                "UPDATE outbox.outbox_events SET published_at = published_at "
                "WHERE false"
            )
        )
        assert result.rowcount == 0


async def test_relay_cannot_insert_outbox(relay_engine: AsyncEngine) -> None:
    """relay 不能 INSERT 事件（事件只能由业务域同事务写入）。"""
    import uuid as _uuid
    from datetime import UTC, datetime

    async with relay_engine.begin() as conn:
        with pytest.raises(Exception) as exc_info:
            await conn.execute(
                text(
                    "INSERT INTO outbox.outbox_events "
                    "(event_id, aggregate_type, aggregate_id, tenant_id, "
                    " event_type, payload, occurred_at) "
                    "VALUES (:id, 'Test', 't', :tid, 'TEST_EVENT', '{}', :now)"
                ),
                {
                    "id": _uuid.uuid4(),
                    "tid": _uuid.uuid4(),
                    "now": datetime.now(UTC),
                },
            )
    assert "permission denied" in str(exc_info.value).lower()


async def test_relay_cannot_delete_outbox(relay_engine: AsyncEngine) -> None:
    """relay 不能 DELETE 事件（append-only 审计不可物理删除）。"""
    async with relay_engine.begin() as conn:
        with pytest.raises(Exception) as exc_info:
            await conn.execute(
                text("DELETE FROM outbox.outbox_events WHERE false")
            )
    assert "permission denied" in str(exc_info.value).lower()


async def test_relay_cannot_access_project_planning(relay_engine: AsyncEngine) -> None:
    """relay 没有 project_planning schema 权限（越界读业务表被拒绝）。"""
    async with relay_engine.connect() as conn:
        with pytest.raises(Exception) as exc_info:
            await conn.execute(
                text("SELECT count(*) FROM project_planning.projects")
            )
    assert "permission denied" in str(exc_info.value).lower()


async def test_relay_cannot_access_asset_service(relay_engine: AsyncEngine) -> None:
    """relay 没有 asset_service schema 权限（越界写业务表被拒绝）。"""
    async with relay_engine.connect() as conn:
        with pytest.raises(Exception) as exc_info:
            await conn.execute(
                text("SELECT count(*) FROM asset_service.asset_versions")
            )
    assert "permission denied" in str(exc_info.value).lower()
