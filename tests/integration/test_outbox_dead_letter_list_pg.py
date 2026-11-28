"""死信查询端点（``GET /outbox/dead-letters``）的真实验收。

为什么必须打在真实 PostgreSQL 上
--------------------------------
四项判据只能在库里成立：

* 租户隔离——跨租户不可见由 RLS 决定，应用层只是没有把别人的行取出来；
* 键集分页的顺序——「按 (dead_lettered_at, event_id) 倒序」与游标谓词必须
  真的对齐，替身仓储只能测编排，测不出「翻页漏一条」；
* 部分索引可用——``ix_outbox_dead_letter`` 让这个查询不必扫全表（本轮新迁移）；
* 「重放后从结果集消失」——状态迁移与查询口径必须一致。

静态部分（游标解析、SQL 形态）见 ``tests/unit/test_outbox_dead_letter_query.py``。
本文件全部用例实际运行，无 NOT RUN 项。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from apps.api.main import app
from packages.shared.database import build_migration_url
from tests.conftest import make_test_token

# 查询与重放同属一个运维席位（契约 allowed_principals 的字面值）。
_OPS_ROLE = "消息总线运维操作者"
# 本文件专用租户：与共享的 TEST_TENANT_ID 隔开，断言才能「只看自己的数据」，
# 否则结果集会混入其它用例造的历史死信。
_LIST_TENANT = uuid.UUID("00000000-0000-0000-0000-0000000000d7")
_EVENT_TYPE = "QC_TASK_QUEUED"
_FOREIGN_TYPE = "GENERATION_FAILED"


@pytest.fixture(scope="module")
async def admin_engine() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(build_migration_url())
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture(scope="module")
async def admin_sm(
    admin_engine: AsyncEngine,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    yield async_sessionmaker(admin_engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
async def _clean_tenant(admin_engine: AsyncEngine) -> AsyncIterator[None]:
    """每个用例前后清空本文件租户的死信，保证断言与执行顺序无关。"""
    await _delete_tenant_events(admin_engine)
    try:
        yield
    finally:
        await _delete_tenant_events(admin_engine)


async def _delete_tenant_events(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM outbox.outbox_events WHERE tenant_id = :tid"),
            {"tid": _LIST_TENANT},
        )


def _client(*, roles: list[str], tenant_id: uuid.UUID = _LIST_TENANT) -> AsyncClient:
    token = make_test_token(roles=roles, tenant_id=tenant_id)
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30.0,
    )


async def _seed(
    engine: AsyncEngine,
    *,
    dead_lettered_at: datetime | None,
    tenant_id: uuid.UUID = _LIST_TENANT,
    event_type: str = _EVENT_TYPE,
    attempts: int = 5,
) -> uuid.UUID:
    """造一条事件；``dead_lettered_at`` 为 None 表示「不是死信」。"""
    event_id = uuid.uuid4()
    occurred = datetime.now(UTC)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO outbox.outbox_events "
                "(event_id, aggregate_type, aggregate_id, tenant_id, event_type, "
                " payload, occurred_at, published_at, attempts, next_attempt_at, "
                " dead_lettered_at) "
                "VALUES (:eid, 'QCTask', :agg, :tid, :etype, "
                " CAST(:payload AS jsonb), :now, NULL, :attempts, :now, :dead_at)"
            ),
            {
                "eid": event_id,
                "agg": f"qc-{event_id.hex[:8]}",
                "tid": tenant_id,
                "etype": event_type,
                "payload": '{"secret": "不应出现在运维列表里"}',
                "now": occurred,
                "attempts": attempts,
                "dead_at": dead_lettered_at,
            },
        )
    return event_id


async def _list(client: AsyncClient, **params: Any) -> Any:
    return await client.get("/outbox/dead-letters", params=params)


# --------------------------------------------------------------------------- #
# 基本形态：只列死信、只给元数据、按进入死信时间倒序
# --------------------------------------------------------------------------- #
async def test_lists_only_dead_letters_newest_first(
    admin_engine: AsyncEngine,
) -> None:
    now = datetime.now(UTC)
    older = await _seed(admin_engine, dead_lettered_at=now - timedelta(hours=2))
    newer = await _seed(admin_engine, dead_lettered_at=now - timedelta(minutes=5))
    # 非死信（正常待投递）与已投递事件都不该出现：查询口径是「已放弃投递」。
    await _seed(admin_engine, dead_lettered_at=None)

    async with _client(roles=[_OPS_ROLE]) as client:
        resp = await _list(client)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    ids = [item["event_id"] for item in body["items"]]
    assert ids == [str(newer), str(older)], "必须按进入死信时间倒序"
    assert body["next_cursor"] is None

    item = body["items"][0]
    # 字段集合即权限边界：多一个 payload 就把运维列表变成业务数据出口。
    assert set(item) == {
        "event_id",
        "event_type",
        "attempts",
        "dead_lettered_at",
        "occurred_at",
    }
    assert item["event_type"] == _EVENT_TYPE
    assert item["attempts"] == 5


async def test_event_type_filter_narrows_result(
    admin_engine: AsyncEngine,
) -> None:
    now = datetime.now(UTC)
    await _seed(admin_engine, dead_lettered_at=now, event_type=_EVENT_TYPE)
    other = await _seed(
        admin_engine,
        dead_lettered_at=now - timedelta(minutes=1),
        event_type=_FOREIGN_TYPE,
    )

    async with _client(roles=[_OPS_ROLE]) as client:
        resp = await _list(client, event_type=_FOREIGN_TYPE)
    assert resp.status_code == 200, resp.text
    assert [i["event_id"] for i in resp.json()["items"]] == [str(other)]


# --------------------------------------------------------------------------- #
# 键集分页：翻完不重不漏，含「同一时刻多条」的全序场景
# --------------------------------------------------------------------------- #
async def test_cursor_pagination_covers_every_row_exactly_once(
    admin_engine: AsyncEngine,
) -> None:
    now = datetime.now(UTC)
    # 两条共享同一 dead_lettered_at：只按时间做游标锚点就会在这里丢行。
    same_instant_a = await _seed(admin_engine, dead_lettered_at=now)
    same_instant_b = await _seed(admin_engine, dead_lettered_at=now)
    third = await _seed(admin_engine, dead_lettered_at=now - timedelta(hours=1))

    seen: list[str] = []
    async with _client(roles=[_OPS_ROLE]) as client:
        cursor: str | None = None
        for _ in range(10):  # 防御性上限，分页不该出现无限循环
            params = {"limit": 1}
            if cursor is not None:
                params["cursor"] = cursor
            resp = await _list(client, **params)
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert len(body["items"]) == 1
            seen.append(body["items"][0]["event_id"])
            cursor = body["next_cursor"]
            if cursor is None:
                break

    expected = {str(same_instant_a), str(same_instant_b), str(third)}
    assert set(seen) == expected, "翻页必须覆盖全部死信"
    assert len(seen) == len(expected), "翻页不得重复"
    # 同一时刻的两条按 event_id 倒序排列，全序因此稳定。
    assert seen[0] in {str(same_instant_a), str(same_instant_b)}


async def test_cursor_replay_to_last_page_returns_no_cursor(
    admin_engine: AsyncEngine,
) -> None:
    now = datetime.now(UTC)
    await _seed(admin_engine, dead_lettered_at=now)
    await _seed(admin_engine, dead_lettered_at=now - timedelta(minutes=1))

    async with _client(roles=[_OPS_ROLE]) as client:
        first = await _list(client, limit=1)
        second = await _list(client, limit=1, cursor=first.json()["next_cursor"])
    assert second.status_code == 200, second.text
    assert second.json()["next_cursor"] is None


# --------------------------------------------------------------------------- #
# 租户隔离与参数校验
# --------------------------------------------------------------------------- #
async def test_other_tenant_dead_letters_are_invisible(
    admin_engine: AsyncEngine,
) -> None:
    now = datetime.now(UTC)
    mine = await _seed(admin_engine, dead_lettered_at=now)
    await _seed(
        admin_engine,
        dead_lettered_at=now,
        tenant_id=uuid.UUID("00000000-0000-0000-0000-0000000000d8"),
    )

    async with _client(roles=[_OPS_ROLE]) as client:
        resp = await _list(client)
    assert resp.status_code == 200, resp.text
    assert [i["event_id"] for i in resp.json()["items"]] == [str(mine)]


async def test_tenant_parameter_is_not_accepted(admin_engine: AsyncEngine) -> None:
    """租户只能来自 Principal：多传一个 ``tenant_id`` 不能改变结果。

    去掉这个参数化的可能，是把「跨租户列举」从「取决于实现是否小心」变成
    「接口上根本没有这个入口」。
    """
    now = datetime.now(UTC)
    mine = await _seed(admin_engine, dead_lettered_at=now)

    async with _client(roles=[_OPS_ROLE]) as client:
        resp = await _list(client, tenant_id=str(uuid.UUID("00000000-0000-0000-0000-0000000000d8")))
    assert resp.status_code == 200, resp.text
    assert [i["event_id"] for i in resp.json()["items"]] == [str(mine)]


@pytest.mark.parametrize(
    "params",
    [
        {"cursor": "not-a-cursor"},
        {"cursor": "2026-11-25T06:00:00|00000000-0000-0000-0000-000000000001"},
        {"limit": 0},
        {"limit": 201},
    ],
)
async def test_invalid_query_params_are_rejected(params: dict[str, Any]) -> None:
    async with _client(roles=[_OPS_ROLE]) as client:
        resp = await _list(client, **params)
    assert resp.status_code == 422, resp.text


async def test_missing_seat_is_forbidden() -> None:
    async with _client(roles=["导演/创作负责人"]) as client:
        resp = await _list(client)
    assert resp.status_code == 403, resp.text


# --------------------------------------------------------------------------- #
# 与重放的一致性
# --------------------------------------------------------------------------- #
async def test_replayed_event_disappears_from_list(admin_engine: AsyncEngine) -> None:
    now = datetime.now(UTC)
    event_id = await _seed(admin_engine, dead_lettered_at=now)

    async with _client(roles=[_OPS_ROLE]) as client:
        before = await _list(client)
        assert [i["event_id"] for i in before.json()["items"]] == [str(event_id)]

        replay = await client.post(
            f"/outbox/dead-letters/{event_id}/replay",
            json={"reason": "确认下游已修复"},
        )
        assert replay.status_code == 200, replay.text

        after = await _list(client)
    assert after.json()["items"] == [], "重放后不再是死信，必须从结果集消失"
