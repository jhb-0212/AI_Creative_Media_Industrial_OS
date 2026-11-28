"""数据库角色边界负向测试（F03 数据所有权：进程边界隔离）。

验证（需真实 PostgreSQL，迁移 7a8b9c0d1e2f / b9e1c2d3f4a5 / c1f2e3d4a5b6 已应用）：
1. outbox_relay_role 能 SELECT，并能 UPDATE **投递元数据列**
   （published_at / attempts / next_attempt_at / dead_lettered_at）。
2. outbox_relay_role 不能 INSERT / DELETE outbox.outbox_events（事件只能由
   业务域同事务写入，relay 不得伪造/删除审计证据）。
3. outbox_relay_role **不能 UPDATE 事件业务内容**（payload / event_type /
   tenant_id / aggregate_id）——列级授权，审查 P1-01。
4. outbox_relay_role 没有 project_planning / asset_service 等业务 schema 的
   权限（越界写业务表被数据库层拒绝，而非依赖代码自觉）。
5. 旧 app_role 不再持有 outbox 的写权限（审查 P1-02 / R9：旧角色生命周期
   闭环，不得成为绕过新域角色边界的入口）。

这是「数据所有权下沉到数据库权限边界」的负向证明：即使 Relay 代码
被攻破或误改，它也无法写业务数据、无法篡改事件内容。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from packages.shared.database import build_database_url, build_relay_database_url

_RELAY_ENGINE: AsyncEngine | None = None


@pytest.fixture(scope="module")
async def relay_engine() -> AsyncIterator[AsyncEngine]:
    """outbox_relay_role 连接（Relay 运行时的真实连接角色）。"""
    engine = create_async_engine(build_relay_database_url())
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture(scope="module")
async def app_engine() -> AsyncIterator[AsyncEngine]:
    """旧 app_role 连接（验证其 outbox 写权限已被回收）。"""
    engine = create_async_engine(build_database_url())
    try:
        yield engine
    finally:
        await engine.dispose()


async def test_relay_can_select_outbox(relay_engine: AsyncEngine) -> None:
    """relay 能读 outbox_events（领取待投递事件的前提）。"""
    async with relay_engine.connect() as conn:
        rows = await conn.execute(text("SELECT count(*) FROM outbox.outbox_events WHERE false"))
        assert rows.scalar() == 0


async def test_relay_can_update_published_at(relay_engine: AsyncEngine) -> None:
    """relay 能 UPDATE 投递元数据列（回写 published_at 等）。"""
    async with relay_engine.begin() as conn:
        # 空更新：WHERE false 命中 0 行，仅验证 UPDATE 权限存在、不产生副作用。
        result = await conn.execute(
            text(
                "UPDATE outbox.outbox_events "
                "SET published_at = published_at, attempts = attempts, "
                "    next_attempt_at = next_attempt_at, "
                "    dead_lettered_at = dead_lettered_at "
                "WHERE false"
            )
        )
        assert result.rowcount == 0


async def test_relay_cannot_update_event_content(relay_engine: AsyncEngine) -> None:
    """relay 不能改事件业务内容（审查 P1-01：审计事实不可篡改）。

    整表 UPDATE 会让被攻破的 relay 改写 payload / event_type / tenant_id，
    使已投递审计事件失去可信度。列级授权后，改这些列必须在数据库层报
    permission denied for column。
    """
    for column, value in (
        ("payload", "'{\"tampered\": true}'"),
        ("event_type", "'TAMPERED'"),
        ("tenant_id", "gen_random_uuid()"),
        ("aggregate_id", "'tampered'"),
    ):
        async with relay_engine.begin() as conn:
            with pytest.raises(Exception) as exc_info:
                await conn.execute(
                    text(f"UPDATE outbox.outbox_events SET {column} = {value} WHERE false")
                )
        message = str(exc_info.value).lower()
        assert "permission denied" in message, f"{column} 未被拒绝：{message}"


async def test_relay_column_privileges_match_contract(relay_engine: AsyncEngine) -> None:
    """列级授权口径与 RELAY_UPDATE_COLUMNS 精确一致（防止悄悄放大）。

    ``has_table_privilege(..., 'UPDATE')`` 不能区分列级授权，必须逐列用
    ``has_column_privilege`` 判定——否则「整表 UPDATE 被改回来」也能蒙混过关。
    """
    from packages.shared.partition_guard import RELAY_UPDATE_COLUMNS

    async with relay_engine.connect() as conn:
        columns = (
            (
                await conn.execute(
                    text(
                        """
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_schema = 'outbox' AND table_name = 'outbox_events'
                    """
                    )
                )
            )
            .scalars()
            .all()
        )

        for column in columns:
            granted = (
                await conn.execute(
                    text(
                        "SELECT has_column_privilege("
                        "current_user, 'outbox.outbox_events', :col, 'UPDATE')"
                    ),
                    {"col": column},
                )
            ).scalar_one()
            expected = column in RELAY_UPDATE_COLUMNS
            assert granted is expected, (
                f"{column} 的 UPDATE 授权与契约不符：实际 {granted}，期望 {expected}"
            )


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
            await conn.execute(text("DELETE FROM outbox.outbox_events WHERE false"))
    assert "permission denied" in str(exc_info.value).lower()


async def test_relay_cannot_access_project_planning(relay_engine: AsyncEngine) -> None:
    """relay 没有 project_planning schema 权限（越界读业务表被拒绝）。"""
    async with relay_engine.connect() as conn:
        with pytest.raises(Exception) as exc_info:
            await conn.execute(text("SELECT count(*) FROM project_planning.projects"))
    assert "permission denied" in str(exc_info.value).lower()


async def test_relay_cannot_access_asset_service(relay_engine: AsyncEngine) -> None:
    """relay 没有 asset_service schema 权限（越界写业务表被拒绝）。"""
    async with relay_engine.connect() as conn:
        with pytest.raises(Exception) as exc_info:
            await conn.execute(text("SELECT count(*) FROM asset_service.asset_versions"))
    assert "permission denied" in str(exc_info.value).lower()


async def test_legacy_app_role_cannot_write_outbox(app_engine: AsyncEngine) -> None:
    """旧 app_role 不得再写事件（审查 P1-02 / R9：旧角色成为绕过入口）。

    拆域角色后 app_role 不再承担任何生产写路径，但它仍残留 outbox 的
    INSERT/UPDATE——那是一条绕过 project_planning_role / asset_service_role
    直接伪造审计事件的入口。权限层面断言（口径）+ 真实执行（行为）双证。
    """
    async with app_engine.connect() as conn:
        for privilege in ("INSERT", "UPDATE", "DELETE"):
            granted = (
                await conn.execute(
                    text("SELECT has_table_privilege(current_user, 'outbox.outbox_events', :p)"),
                    {"p": privilege},
                )
            ).scalar_one()
            assert granted is False, f"app_role 仍持有 outbox {privilege} 权限"

    # 行为证明：真实 INSERT 必须被拒绝（即使当前实现永远不走这条路）。
    import uuid as _uuid
    from datetime import UTC, datetime

    async with app_engine.begin() as conn:
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
