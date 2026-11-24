"""outbox_ops_role 的权限边界负向测试（D6-4 的数据库层证明）。

这条角色的权限面被刻意收窄到三句话：

1. 能**读**事件（``SELECT outbox_events``）——判定「是不是死信」必须先看事实；
2. 只能改**投递状态三列**（``attempts`` / ``dead_lettered_at`` /
   ``next_attempt_at``）——不含 ``published_at``（投递时间只能由真正投递成功的
   relay 写）、不含 ``payload`` / ``event_type``（事件内容是不可变事实）；
3. 能**写审计表**（``dead_letter_replays`` 的 ``SELECT/INSERT``），但没有
   ``UPDATE/DELETE/TRUNCATE``——append-only 在授权层与触发器层同时成立。

为什么不能只靠应用层自觉
------------------------
「重放不改事件内容」这条规则如果只写在 service 里，那么任何拿到这条角色口令的
代码（新脚本、临时排障、将来的一次重构）都能顺手把 ``published_at`` 写成
``now()``，让一条投递失败的事件看起来「已成功」。授权边界必须在库里。

无真实 PostgreSQL 时本文件不适用；CI 里它是必跑项。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    create_async_engine,
)

from packages.shared.database import (
    build_migration_url,
    build_outbox_ops_database_url,
)
from packages.shared.rls import RLS_TENANT_SETTING
from tests.conftest import TEST_TENANT_ID


@pytest.fixture(scope="module")
async def ops_engine() -> AsyncIterator[AsyncEngine]:
    """运维角色的真实连接（与 API 运行时同一条 DSN）。"""
    engine = create_async_engine(build_outbox_ops_database_url())
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture(scope="module")
async def admin_engine() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(build_migration_url())
    try:
        yield engine
    finally:
        await engine.dispose()


def _is_permission_denied(exc: BaseException) -> bool:
    return "permission denied" in str(exc).lower()


async def _set_tenant(
    conn: AsyncConnection, tenant_id: uuid.UUID = TEST_TENANT_ID
) -> None:
    """在事务内注入租户上下文（``true`` = 事务结束即失效）。"""
    await conn.execute(
        text("SELECT set_config(:key, :value, true)"),
        {"key": RLS_TENANT_SETTING, "value": str(tenant_id)},
    )


# --------------------------------------------------------------------------- #
# 角色属性：不是超级用户、不绕过 RLS
# --------------------------------------------------------------------------- #
async def test_role_is_not_privileged(ops_engine: AsyncEngine) -> None:
    """角色属性必须是 NOSUPERUSER / NOBYPASSRLS：否则上面的授权全是纸面约束。"""
    async with ops_engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT rolsuper, rolbypassrls, rolcreatedb, rolcreaterole "
                    "FROM pg_roles WHERE rolname = current_user"
                )
            )
        ).one()
    assert row.rolsuper is False
    assert row.rolbypassrls is False
    assert row.rolcreatedb is False
    assert row.rolcreaterole is False


# --------------------------------------------------------------------------- #
# 能做的：读事件、清死信三列、写审计
# --------------------------------------------------------------------------- #
async def test_can_read_events(ops_engine: AsyncEngine) -> None:
    async with ops_engine.connect() as conn:
        assert (
            await conn.execute(
                text("SELECT count(*) FROM outbox.outbox_events WHERE false")
            )
        ).scalar() == 0


async def test_can_clear_three_delivery_columns(ops_engine: AsyncEngine) -> None:
    """空更新：命中 0 行，仅证明三列 UPDATE 权限存在且不产生副作用。"""
    async with ops_engine.begin() as conn:
        await _set_tenant(conn)
        result = await conn.execute(
            text(
                "UPDATE outbox.outbox_events "
                "SET attempts = attempts, dead_lettered_at = dead_lettered_at, "
                "    next_attempt_at = next_attempt_at "
                "WHERE false"
            )
        )
        assert result.rowcount == 0


async def test_can_insert_and_read_own_audit_rows(ops_engine: AsyncEngine) -> None:
    """审计表可写可读（否则重放动作本身无法留痕）。"""
    replay_id = uuid.uuid4()
    async with ops_engine.begin() as conn:
        await _set_tenant(conn)
        await conn.execute(
            text(
                "INSERT INTO outbox.dead_letter_replays "
                "(replay_id, source_event_id, tenant_id, event_type, operator, "
                " reason, attempts_before, dead_lettered_at_before, result) "
                "VALUES (:rid, :eid, :tid, 'QC_TASK_QUEUED', 'probe', '权限探针', "
                " 5, :dead_at, 'SUCCEEDED')"
            ),
            {
                "rid": replay_id,
                "eid": uuid.uuid4(),
                "tid": TEST_TENANT_ID,
                "dead_at": datetime.now(UTC),
            },
        )
        found = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM outbox.dead_letter_replays "
                    "WHERE replay_id = :rid"
                ),
                {"rid": replay_id},
            )
        ).scalar()
    assert found == 1


# --------------------------------------------------------------------------- #
# 不能做的：伪造投递成功、伪造业务事件、改写或删除历史
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "assignment",
    [
        "published_at = now()",
        "payload = '{}'::jsonb",
        "event_type = 'FORGED'",
        "tenant_id = gen_random_uuid()",
        "aggregate_id = 'forged'",
        "occurred_at = now()",
    ],
)
async def test_cannot_forge_event_content_or_delivery_time(
    ops_engine: AsyncEngine, assignment: str
) -> None:
    """列级授权的负向：这六列每一列写成功都等于推翻一条不变量。

    ``published_at`` 最危险：写成 ``now()`` 会让一条投递失败的事件看起来
    「已成功」，下游再也等不到它，而报表与告警都会显示一切正常。
    """
    with pytest.raises(DBAPIError) as exc:
        async with ops_engine.begin() as conn:
            await _set_tenant(conn)
            await conn.execute(
                text(f"UPDATE outbox.outbox_events SET {assignment} WHERE false")
            )
    assert _is_permission_denied(exc.value), str(exc.value)


async def test_cannot_insert_business_events(ops_engine: AsyncEngine) -> None:
    """运维主体不得写事件总线：补上 INSERT 等于允许伪造任意业务事件。"""
    with pytest.raises(DBAPIError) as exc:
        async with ops_engine.begin() as conn:
            await _set_tenant(conn)
            await conn.execute(
                text(
                    "INSERT INTO outbox.outbox_events "
                    "(event_id, aggregate_type, aggregate_id, tenant_id, "
                    " event_type, payload, occurred_at) "
                    "VALUES (gen_random_uuid(), 'QCTask', 'forged', :tid, "
                    " 'QC_TASK_QUEUED', '{}'::jsonb, now())"
                ),
                {"tid": TEST_TENANT_ID},
            )
    assert _is_permission_denied(exc.value), str(exc.value)


async def test_cannot_delete_events(ops_engine: AsyncEngine) -> None:
    """审计证据不可删除：死信不删行，「投递放弃」本身是可审计事实。"""
    with pytest.raises(DBAPIError) as exc:
        async with ops_engine.begin() as conn:
            await _set_tenant(conn)
            await conn.execute(text("DELETE FROM outbox.outbox_events WHERE false"))
    assert _is_permission_denied(exc.value), str(exc.value)


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE outbox.dead_letter_replays SET reason = 'rewritten' WHERE false",
        "DELETE FROM outbox.dead_letter_replays WHERE false",
        "TRUNCATE outbox.dead_letter_replays",
    ],
)
async def test_audit_table_is_append_only_by_grant(
    ops_engine: AsyncEngine, statement: str
) -> None:
    """授权层就没有 UPDATE/DELETE/TRUNCATE——第一道防线，先于触发器生效。"""
    with pytest.raises(DBAPIError) as exc:
        async with ops_engine.begin() as conn:
            await _set_tenant(conn)
            await conn.execute(text(statement))
    assert _is_permission_denied(exc.value), str(exc.value)


async def test_cannot_touch_business_schemas(ops_engine: AsyncEngine) -> None:
    """越界：运维角色对业务域表毫无权限（它不是「更强的 app_role」）。"""
    with pytest.raises(DBAPIError) as exc:
        async with ops_engine.connect() as conn:
            await conn.execute(text("SELECT count(*) FROM project_planning.projects"))
    assert _is_permission_denied(exc.value), str(exc.value)


async def test_partitions_carry_the_same_grant_shape(
    admin_engine: AsyncEngine,
) -> None:
    """每个 outbox 分区都必须与父表同形授权。

    分区名按月生成，静态迁移无法枚举——这条守的是「生成器真的覆盖了现存分区」。
    漏一个分区的后果是：那一个月里进入死信的事件无法被重放（且只在那个月发生）。
    """
    async with admin_engine.connect() as conn:
        partitions = [
            row.relname
            for row in await conn.execute(
                text(
                    "SELECT c.relname FROM pg_class c "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = 'outbox' AND c.relkind = 'r' "
                    "AND c.relispartition ORDER BY 1"
                )
            )
        ]
        assert partitions, "outbox_events 必须至少有一个分区（月分区表）"
        for partition in partitions:
            granted = {
                row.column_name
                for row in await conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.column_privileges "
                        "WHERE table_schema = 'outbox' AND table_name = :t "
                        "AND grantee = 'outbox_ops_role' AND privilege_type = 'UPDATE'"
                    ),
                    {"t": partition},
                )
            }
            assert granted == {"attempts", "dead_lettered_at", "next_attempt_at"}, (
                f"分区 {partition} 的列级 UPDATE 授权与父表不同形: {sorted(granted)}"
            )
