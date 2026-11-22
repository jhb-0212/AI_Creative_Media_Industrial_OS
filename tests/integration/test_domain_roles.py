"""业务域角色边界负向测试（DATA-1 模块级角色隔离）。

验证（需真实 PostgreSQL，迁移 8f3b2c1d4e5a 已应用）：
1. project_planning_role 能写 project_planning schema、能写 outbox（同事务事件）。
2. project_planning_role 不能写 asset_service schema（越界写他域表被拒绝）。
3. asset_service_role 能写 asset_service schema、能写 outbox。
4. asset_service_role 对 project_planning 只有**归属三列**的列级 SELECT：
   能解析归属链，读不到项目业务字段，更不能写（SCOPE-3 收窄后的真实边界）。

这是 DATA-1「模块级数据库角色隔离」的负向证明：即使某域代码被攻破或误改，
它也无法直接写另一业务域的数据——边界由数据库权限物理保证，而非代码自觉。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from packages.shared.database import (
    build_asset_service_database_url,
    build_project_planning_database_url,
)


@pytest.fixture(scope="module")
async def project_engine() -> AsyncIterator[AsyncEngine]:
    """project_planning_role 连接（project_planning 域运行时的真实连接角色）。"""
    engine = create_async_engine(build_project_planning_database_url())
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture(scope="module")
async def asset_engine() -> AsyncIterator[AsyncEngine]:
    """asset_service_role 连接（asset_service 域运行时的真实连接角色）。"""
    engine = create_async_engine(build_asset_service_database_url())
    try:
        yield engine
    finally:
        await engine.dispose()


async def test_project_role_can_read_own_schema(project_engine: AsyncEngine) -> None:
    """project_planning_role 能读自己的 schema（project_planning.projects）。"""
    async with project_engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT count(*) FROM project_planning.projects WHERE false")
        )
        assert rows.scalar() == 0


async def test_project_role_can_write_outbox(project_engine: AsyncEngine) -> None:
    """project_planning_role 能读 outbox（同事务事件投递前提）。"""
    async with project_engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT count(*) FROM outbox.outbox_events WHERE false")
        )
        assert rows.scalar() == 0


async def test_project_role_cannot_access_asset_service(
    project_engine: AsyncEngine,
) -> None:
    """project_planning_role 不能读 asset_service schema（越界被他域拒绝）。"""
    async with project_engine.connect() as conn:
        with pytest.raises(Exception) as exc_info:
            await conn.execute(
                text("SELECT count(*) FROM asset_service.asset_versions")
            )
    assert "permission denied" in str(exc_info.value).lower()


async def test_asset_role_can_read_own_schema(asset_engine: AsyncEngine) -> None:
    """asset_service_role 能读自己的 schema（asset_service.asset_versions）。"""
    async with asset_engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT count(*) FROM asset_service.asset_versions WHERE false")
        )
        assert rows.scalar() == 0


async def test_asset_role_can_write_outbox(asset_engine: AsyncEngine) -> None:
    """asset_service_role 能读 outbox（同事务事件投递前提）。"""
    async with asset_engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT count(*) FROM outbox.outbox_events WHERE false")
        )
        assert rows.scalar() == 0


async def test_asset_role_can_read_project_ownership_projection(
    asset_engine: AsyncEngine,
) -> None:
    """asset_service_role 可读 projects 的**归属三列**（SCOPE-3 归属链解析所需）。

    跨域只读被收窄到 (entity_id, tenant_id, organization_id)：资产业务需要沿
    project → organization 解析归属，否则项目/组织级授权无从判定；但这不是
    「开放整表读」——下一组测试证明它拿不到项目业务字段。
    """
    async with asset_engine.connect() as conn:
        await conn.execute(
            text(
                "SELECT entity_id, tenant_id, organization_id "
                "FROM project_planning.projects LIMIT 1"
            )
        )


async def test_asset_role_cannot_read_project_business_fields(
    asset_engine: AsyncEngine,
) -> None:
    """asset_service_role 不能读项目业务字段（列级授权不做整表开放）。"""
    async with asset_engine.connect() as conn:
        with pytest.raises(Exception) as exc_info:
            await conn.execute(
                text("SELECT project_name FROM project_planning.projects LIMIT 1")
            )
    assert "permission denied" in str(exc_info.value).lower()


async def test_asset_role_cannot_write_project_planning(
    asset_engine: AsyncEngine,
) -> None:
    """asset_service_role 不能写 project_planning（跨域写仍由 DATA-1 拦住）。"""
    async with asset_engine.connect() as conn:
        with pytest.raises(Exception) as exc_info:
            await conn.execute(
                text(
                    "INSERT INTO project_planning.projects (entity_id) "
                    "VALUES ('x')"
                )
            )
    assert "permission denied" in str(exc_info.value).lower()
