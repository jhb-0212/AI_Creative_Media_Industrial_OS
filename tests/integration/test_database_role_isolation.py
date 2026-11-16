"""数据库运行角色隔离的负向测试（B1 验收：跨模块写表在数据库层被拒）。

要证明的不是"代码没这么写"，而是"这样写会被数据库拒绝"：

1. 项目规划域角色写不了资产表；资产域角色写不了项目表；
2. 投递域角色写不了任何业务表，也改不了事件正文（只能改投递状态列）；
3. 身份域角色读不到业务数据（吊销表之外一概无权）；
4. 没有任何应用角色能物理删除审计事件（append-only）；
5. 旧的跨 schema 角色 ``app_role`` 已不存在——回退路径被移除；
6. 所有应用角色都是 LOGIN + NOSUPERUSER + NOBYBYPASSRLS（RLS 才真正生效）。

这些断言直接查系统目录（``pg_roles`` / ``has_table_privilege``）而不是跑
业务代码：授权是数据库状态，测业务路径只能证明"某条路径没被走到"，
证明不了"别的路径走不通"。
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.shared.database import build_migration_url
from packages.shared.db_roles import DatabaseDomain, all_business_roles, role_for

pytestmark = pytest.mark.asyncio


def _denied(exc_info: pytest.ExceptionInfo[DBAPIError]) -> bool:
    """是否是"权限不足"这一类失败（而不是别的 SQL 错误）。"""
    message = str(exc_info.value).lower()
    return "permission denied" in message or "insufficient privilege" in message


async def _insert_project(project_sm: async_sessionmaker[AsyncSession]) -> str:
    """用项目规划域写入一条项目行（后续测试把它当作"别人的数据"）。"""
    entity_id = f"role_probe_{uuid.uuid4().hex[:8]}"
    tenant = uuid.uuid4()
    async with project_sm() as session:
        await session.execute(
            text("SELECT set_config('app.current_tenant_id', :t, true)"),
            {"t": str(tenant)},
        )
        await session.execute(
            text(
                """
                INSERT INTO project_planning.projects
                    (entity_id, entity_type, schema_version, entity_version, status,
                     owner_module, tenant_id, created_at, created_by, updated_at,
                     project_name, project_tier)
                VALUES
                    (:entity_id, 'Project', 205, 1, 'DRAFT', 'project_planning',
                     :tenant_id, now(), 'role_probe', now(), :name, 'B')
                """
            ),
            {"entity_id": entity_id, "tenant_id": tenant, "name": "角色隔离探针"},
        )
        await session.commit()
    return entity_id


async def test_project_role_cannot_write_asset_tables(
    project_sm: async_sessionmaker[AsyncSession],
) -> None:
    """项目规划域角色 UPDATE 资产版本表 → permission denied。"""
    with pytest.raises(DBAPIError) as exc_info:
        async with project_sm() as session:
            await session.execute(
                text(
                    "UPDATE asset_service.asset_versions SET status = 'ARCHIVED' "
                    "WHERE false"
                )
            )
    assert _denied(exc_info)


async def test_asset_role_cannot_write_project_tables(
    asset_sm: async_sessionmaker[AsyncSession],
) -> None:
    """资产域角色 INSERT 项目表 → permission denied（跨模块写被数据库拒绝）。"""
    with pytest.raises(DBAPIError) as exc_info:
        async with asset_sm() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO project_planning.projects
                        (entity_id, entity_type, schema_version, entity_version, status,
                         owner_module, tenant_id, created_at, created_by, updated_at,
                         project_name, project_tier)
                    VALUES
                        ('x', 'Project', 205, 1, 'DRAFT', 'asset_service', :t,
                         now(), 'probe', now(), '越权项目', 'B')
                    """
                ),
                {"t": uuid.uuid4()},
            )
    assert _denied(exc_info)


async def test_relay_role_cannot_write_business_tables(
    relay_sm: async_sessionmaker[AsyncSession],
    project_sm: async_sessionmaker[AsyncSession],
) -> None:
    """投递域角色改项目表 → permission denied（relay 不能改业务数据）。"""
    entity_id = await _insert_project(project_sm)
    with pytest.raises(DBAPIError) as exc_info:
        async with relay_sm() as session:
            await session.execute(
                text(
                    "UPDATE project_planning.projects SET project_name = 'relay 改名' "
                    "WHERE entity_id = :e"
                ),
                {"e": entity_id},
            )
    assert _denied(exc_info)


async def test_relay_role_cannot_rewrite_event_payload(
    relay_sm: async_sessionmaker[AsyncSession],
    project_sm: async_sessionmaker[AsyncSession],
) -> None:
    """投递域角色改事件正文 → permission denied（列级授权，不是代码约定）。"""
    event_id = await _append_event(project_sm)
    with pytest.raises(DBAPIError) as exc_info:
        async with relay_sm() as session:
            await session.execute(
                text(
                    "UPDATE outbox.outbox_events "
                    "SET payload = '{\"tampered\": true}'::jsonb "
                    "WHERE event_id = :e"
                ),
                {"e": event_id},
            )
    assert _denied(exc_info)


async def test_relay_role_can_update_delivery_state(
    relay_sm: async_sessionmaker[AsyncSession],
    project_sm: async_sessionmaker[AsyncSession],
) -> None:
    """投递域角色可以回写投递状态（列级授权的正向证明）。"""
    event_id = await _append_event(project_sm)
    async with relay_sm() as session:
        await session.execute(
            text("SELECT set_config('app.current_tenant_id', :t, true)"),
            {"t": str(_EVENT_TENANT)},
        )
        result = await session.execute(
            text(
                "UPDATE outbox.outbox_events SET published_at = now() "
                "WHERE event_id = :e"
            ),
            {"e": event_id},
        )
        await session.commit()
    assert result.rowcount == 1


async def test_identity_role_cannot_read_business_data(
    identity_sm: async_sessionmaker[AsyncSession],
) -> None:
    """身份域角色读项目表 → permission denied（吊销检查不该顺带拿到业务读权）。"""
    with pytest.raises(DBAPIError) as exc_info:
        async with identity_sm() as session:
            await session.execute(text("SELECT 1 FROM project_planning.projects LIMIT 1"))
    assert _denied(exc_info)


async def test_no_business_role_can_delete_audit_events(
    project_sm: async_sessionmaker[AsyncSession],
    relay_sm: async_sessionmaker[AsyncSession],
    asset_sm: async_sessionmaker[AsyncSession],
) -> None:
    """任何应用角色都删不掉审计事件（append-only 在授权层成立）。"""
    for sessionmaker in (project_sm, relay_sm, asset_sm):
        with pytest.raises(DBAPIError) as exc_info:
            async with sessionmaker() as session:
                await session.execute(
                    text("DELETE FROM outbox.outbox_events WHERE false")
                )
        assert _denied(exc_info)


async def test_legacy_app_role_is_gone(pools_query: async_sessionmaker[AsyncSession]) -> None:
    """旧的跨 schema 角色 app_role 已被删除：不存在"回退到万能角色"的路径。"""
    async with pools_query() as session:
        exists = await session.scalar(
            text("SELECT count(*) FROM pg_roles WHERE rolname = 'app_role'")
        )
    assert exists == 0


async def test_every_business_role_is_least_privilege(
    pools_query: async_sessionmaker[AsyncSession],
) -> None:
    """全部应用角色：可登录、非超级用户、不绕过 RLS。"""
    async with pools_query() as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT rolname, rolcanlogin, rolsuper, rolbypassrls
                    FROM pg_roles WHERE rolname = ANY(:names)
                    """
                ),
                {"names": list(all_business_roles())},
            )
        ).mappings().all()

    assert {row["rolname"] for row in rows} == set(all_business_roles())
    for row in rows:
        assert row["rolcanlogin"] is True, f"{row['rolname']} 无法登录"
        assert row["rolsuper"] is False, f"{row['rolname']} 是超级用户"
        assert row["rolbypassrls"] is False, f"{row['rolname']} 绕过 RLS"


async def test_role_mapping_covers_every_domain() -> None:
    """每个业务域都必须有角色名（缺一个域就等于缺一条权限边界）。"""
    for domain in DatabaseDomain:
        assert role_for(domain).startswith("svc_")


# --- 事件写入辅助 ---------------------------------------------------------

_EVENT_TENANT = uuid.uuid4()


async def _append_event(project_sm: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    """用项目规划域追加一条 outbox 事件（业务域有权写 outbox，这是设计允许的）。"""
    event_id = uuid.uuid4()
    async with project_sm() as session:
        await session.execute(
            text("SELECT set_config('app.current_tenant_id', :t, true)"),
            {"t": str(_EVENT_TENANT)},
        )
        await session.execute(
            text(
                """
                INSERT INTO outbox.outbox_events
                    (event_id, aggregate_type, aggregate_id, tenant_id, event_type,
                     payload, occurred_at, attempts, next_attempt_at)
                VALUES
                    (:event_id, 'Project', 'role-probe', :tenant_id, 'PROBE',
                     '{"k": 1}'::jsonb, now(), 0, now())
                """
            ),
            {"event_id": event_id, "tenant_id": _EVENT_TENANT},
        )
        await session.commit()
    return event_id


@pytest.fixture(scope="module")
async def pools_query():
    """迁移角色会话工厂：用于读系统目录（应用角色不该有 pg_roles 的可见性假设）。"""
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(build_migration_url())
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest.fixture(scope="module", autouse=True)
async def _cleanup_events() -> None:
    """模块结束时清掉本文件写入的探针事件与项目行（应用角色无 DELETE 权）。"""
    yield
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(build_migration_url())
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "DELETE FROM outbox.outbox_events WHERE aggregate_id = 'role-probe'"
                )
            )
            await conn.execute(
                text(
                    "DELETE FROM project_planning.projects "
                    "WHERE entity_id LIKE 'role_probe_%'"
                )
            )
    finally:
        await engine.dispose()
