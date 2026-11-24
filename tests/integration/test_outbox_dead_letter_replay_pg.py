"""死信人工重放（D6-9）的真实验收：负向、并发、审计与投递语义。

为什么必须打在真实 PostgreSQL 上
------------------------------
六项判据里有五项只能在库里成立：

* D6-4 权限边界——运维角色只有 ``SELECT outbox_events`` + 三列 ``UPDATE``
  + ``dead_letter_replays`` 的 ``SELECT/INSERT``；用替身测等于测了个平行实现；
* D6-6 并发——「锁行 → 条件清标记」的行为依赖 ``FOR UPDATE`` 与 READ COMMITTED
  下的重读语义，没有库就只有先查，而先查在并发下必然漏；
* D6-7 租户隔离——跨租户不可见由 RLS 决定，应用层只是把「查不到」翻译成 404；
* append-only——UPDATE/TRUNCATE 被触发器拒绝，这条只能由数据库自己回答；
* 「交回正常领取」——重放后的事件必须真的落进 relay 的 claim 结果集，
  否则「重放成功」只是标记位好看。

静态测试另行报告（``tests/unit/test_outbox_dead_letter_replay.py``）；
本文件全部用例实际运行，无 NOT RUN 项。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from apps.api.main import app
from packages.shared.database import build_migration_url
from tests.conftest import TEST_TENANT_ID, make_test_token
from workers.relay.consumer import _CLAIM

# 重放的专项席位（action_registry 里 allowed_principals 的字面值）。
_REPLAY_ROLE = "消息总线运维操作者"
_OTHER_TENANT = uuid.UUID("00000000-0000-0000-0000-0000000000d9")
_EVENT_TYPE = "QC_TASK_QUEUED"


@pytest.fixture(scope="module")
async def admin_engine() -> AsyncIterator[AsyncEngine]:
    """超级用户连接：造数与断言都走它（RLS 由应用连接证明，不必在断言里重复）。"""
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


def _client(*, roles: list[str], tenant_id: uuid.UUID = TEST_TENANT_ID) -> AsyncClient:
    """带真实令牌的客户端（走完整验签与授权链，不 mock Principal）。"""
    token = make_test_token(roles=roles, tenant_id=tenant_id)
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30.0,
    )


async def _seed_dead_letter(
    engine: AsyncEngine,
    *,
    tenant_id: uuid.UUID = TEST_TENANT_ID,
    event_type: str = _EVENT_TYPE,
    attempts: int = 5,
    dead_lettered: bool = True,
    published: bool = False,
) -> uuid.UUID:
    """造一条「已放弃投递」的事件（默认状态），返回 event_id。"""
    event_id = uuid.uuid4()
    now = datetime.now(UTC)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO outbox.outbox_events "
                "(event_id, aggregate_type, aggregate_id, tenant_id, event_type, "
                " payload, occurred_at, published_at, attempts, next_attempt_at, "
                " dead_lettered_at) "
                "VALUES (:eid, 'QCTask', :agg, :tid, :etype, "
                " CAST(:payload AS jsonb), :now, :published, :attempts, :next_at, "
                " :dead_at)"
            ),
            {
                "eid": event_id,
                "agg": f"qc-{event_id.hex[:8]}",
                "tid": tenant_id,
                "etype": event_type,
                "payload": '{"qc_task_id": "qc-1", "attempt": 5}',
                "now": now,
                "published": now if published else None,
                "attempts": attempts,
                "next_at": now,
                "dead_at": now if dead_lettered else None,
            },
        )
    return event_id


async def _event_row(engine: AsyncEngine, event_id: uuid.UUID) -> dict[str, Any]:
    async with engine.connect() as conn:
        row = (
            (
                await conn.execute(
                    text(
                        "SELECT event_id, event_type, payload, attempts, "
                        "published_at, dead_lettered_at, next_attempt_at "
                        "FROM outbox.outbox_events WHERE event_id = :eid"
                    ),
                    {"eid": event_id},
                )
            )
            .mappings()
            .first()
        )
    assert row is not None, "事件不应消失：重放不是删除"
    return dict(row)


async def _audit_rows(engine: AsyncEngine, event_id: uuid.UUID) -> list[dict[str, Any]]:
    async with engine.connect() as conn:
        rows = (
            (
                await conn.execute(
                    text(
                        "SELECT replay_id, source_event_id, tenant_id, event_type, "
                        "operator, reason, attempts_before, dead_lettered_at_before, "
                        "result, failure_reason, created_at "
                        "FROM outbox.dead_letter_replays "
                        "WHERE source_event_id = :eid ORDER BY created_at"
                    ),
                    {"eid": event_id},
                )
            )
            .mappings()
            .all()
        )
        return [dict(r) for r in rows]


async def _event_count(engine: AsyncEngine, tenant_id: uuid.UUID) -> int:
    async with engine.connect() as conn:
        return (
            await conn.execute(
                text(
                    "SELECT count(*) FROM outbox.outbox_events WHERE tenant_id = :tid"
                ),
                {"tid": tenant_id},
            )
        ).scalar_one()


async def _claimable_ids(engine: AsyncEngine) -> set[uuid.UUID]:
    """relay 的 claim 结果集（复用生产 SQL，不另写一份等价查询）。

    batch 取「当前可领取事件数 + 余量」而不是固定常数：本用例只关心「刚重放的
    那条在不在结果集里」，固定 500 会让窗口被历史测试数据挤爆而误报失败——
    relay 本身按批领取，窗口大小是它的正常行为，不是缺陷。
    """
    async with engine.connect() as conn:
        pending = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM outbox.outbox_events "
                    "WHERE published_at IS NULL AND dead_lettered_at IS NULL "
                    "AND next_attempt_at <= now()"
                )
            )
        ).scalar_one()
        rows = await conn.execute(_CLAIM, {"batch": pending + 10})
        return {row.event_id for row in rows}


# --------------------------------------------------------------------------- #
# 正向：状态迁回可领取，审计带「重放前」事实，事件身份与内容一字不动
# --------------------------------------------------------------------------- #
async def test_replay_requeues_event_and_records_pre_replay_facts(
    admin_engine: AsyncEngine,
) -> None:
    event_id = await _seed_dead_letter(admin_engine)
    before = await _event_row(admin_engine, event_id)
    count_before = await _event_count(admin_engine, TEST_TENANT_ID)

    async with _client(roles=[_REPLAY_ROLE]) as client:
        resp = await client.post(
            f"/outbox/dead-letters/{event_id}/replay",
            json={"reason": "下游 5xx 已修复，确认可投递"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["event_id"] == str(event_id)
    assert body["event_type"] == _EVENT_TYPE
    assert body["attempts_before"] == 5
    # 重放 ≠ 已投递：响应只承诺「已交回正常领取」。
    assert body["status"] == "REQUEUED"

    after = await _event_row(admin_engine, event_id)
    assert after["dead_lettered_at"] is None, "死信标记必须清掉"
    assert after["attempts"] == 0, "重试计数必须归零，否则下一次失败立刻再进死信"
    assert after["published_at"] is None, "重放不得冒充投递成功"
    # 事件身份与内容不变：event_id 是业务幂等支点，payload 是已发生的事实。
    assert after["event_id"] == before["event_id"]
    assert after["event_type"] == before["event_type"]
    assert after["payload"] == before["payload"]
    assert await _event_count(admin_engine, TEST_TENANT_ID) == count_before, (
        "重放不得复制新事件"
    )

    assert event_id in await _claimable_ids(admin_engine), (
        "重放后的事件必须落进 relay 的 claim 结果集，否则「已交回」只是标记位好看"
    )

    audits = await _audit_rows(admin_engine, event_id)
    assert len(audits) == 1
    audit = audits[0]
    assert audit["tenant_id"] == TEST_TENANT_ID
    assert audit["event_type"] == _EVENT_TYPE
    assert audit["operator"] == "user_script", "操作者取自 Principal，不取请求体"
    assert audit["reason"] == "下游 5xx 已修复，确认可投递"
    assert audit["attempts_before"] == 5
    assert audit["dead_lettered_at_before"] == before["dead_lettered_at"]
    assert audit["result"] == "SUCCEEDED"
    assert audit["failure_reason"] is None


async def test_second_replay_conflicts_and_keeps_one_audit_row(
    admin_engine: AsyncEngine,
) -> None:
    """重放两次：第二次 409，库里仍只有一条审计行、一条事件。"""
    event_id = await _seed_dead_letter(admin_engine)
    async with _client(roles=[_REPLAY_ROLE]) as client:
        first = await client.post(
            f"/outbox/dead-letters/{event_id}/replay", json={"reason": "第一次"}
        )
        second = await client.post(
            f"/outbox/dead-letters/{event_id}/replay", json={"reason": "第二次"}
        )
    assert first.status_code == 200, first.text
    assert second.status_code == 409, second.text
    assert len(await _audit_rows(admin_engine, event_id)) == 1


# --------------------------------------------------------------------------- #
# 负向：五类请求一律拒绝，且不产生任何副作用
# --------------------------------------------------------------------------- #
async def test_unknown_event_is_not_found(admin_engine: AsyncEngine) -> None:
    missing = uuid.uuid4()
    async with _client(roles=[_REPLAY_ROLE]) as client:
        resp = await client.post(
            f"/outbox/dead-letters/{missing}/replay", json={"reason": "不存在"}
        )
    assert resp.status_code == 404, resp.text
    assert await _audit_rows(admin_engine, missing) == []


async def test_cross_tenant_dead_letter_is_invisible(admin_engine: AsyncEngine) -> None:
    """跨租户与不存在同形（404）：不给「别处有这条事件」的探测位。"""
    foreign = await _seed_dead_letter(admin_engine, tenant_id=_OTHER_TENANT)
    async with _client(roles=[_REPLAY_ROLE]) as client:
        resp = await client.post(
            f"/outbox/dead-letters/{foreign}/replay", json={"reason": "跨租户尝试"}
        )
    assert resp.status_code == 404, resp.text
    row = await _event_row(admin_engine, foreign)
    assert row["dead_lettered_at"] is not None, "跨租户尝试不得改动对方的事件"
    assert await _audit_rows(admin_engine, foreign) == []


async def test_live_event_cannot_be_replayed(admin_engine: AsyncEngine) -> None:
    """仍在正常重试中的事件 → 409：清标记会掩盖退避，并抹掉真实失败次数。"""
    event_id = await _seed_dead_letter(admin_engine, attempts=1, dead_lettered=False)
    async with _client(roles=[_REPLAY_ROLE]) as client:
        resp = await client.post(
            f"/outbox/dead-letters/{event_id}/replay", json={"reason": "未死信"}
        )
    assert resp.status_code == 409, resp.text
    row = await _event_row(admin_engine, event_id)
    assert row["attempts"] == 1
    assert await _audit_rows(admin_engine, event_id) == []


async def test_published_event_cannot_be_replayed(admin_engine: AsyncEngine) -> None:
    """已投递成功 → 409：重放不是「再投一次」。"""
    event_id = await _seed_dead_letter(
        admin_engine, dead_lettered=False, published=True
    )
    async with _client(roles=[_REPLAY_ROLE]) as client:
        resp = await client.post(
            f"/outbox/dead-letters/{event_id}/replay", json={"reason": "已投递"}
        )
    assert resp.status_code == 409, resp.text
    assert await _audit_rows(admin_engine, event_id) == []


async def test_undeclared_event_type_is_rejected(admin_engine: AsyncEngine) -> None:
    """契约外事件类型 → 409：重放只会制造永不被消费的积压。"""
    event_id = await _seed_dead_letter(admin_engine, event_type="NOT_DECLARED_ANYWHERE")
    async with _client(roles=[_REPLAY_ROLE]) as client:
        resp = await client.post(
            f"/outbox/dead-letters/{event_id}/replay", json={"reason": "未知类型"}
        )
    assert resp.status_code == 409, resp.text
    row = await _event_row(admin_engine, event_id)
    assert row["dead_lettered_at"] is not None
    assert await _audit_rows(admin_engine, event_id) == []


async def test_other_role_cannot_use_replay_entry(admin_engine: AsyncEngine) -> None:
    """专项席位：业务域角色即便能读 outbox，也不得重放。"""
    event_id = await _seed_dead_letter(admin_engine)
    async with _client(roles=["QC 审核人"]) as client:
        resp = await client.post(
            f"/outbox/dead-letters/{event_id}/replay", json={"reason": "越权尝试"}
        )
    assert resp.status_code == 403, resp.text
    row = await _event_row(admin_engine, event_id)
    assert row["dead_lettered_at"] is not None
    assert await _audit_rows(admin_engine, event_id) == []


async def test_body_cannot_smuggle_operator_or_tenant(
    admin_engine: AsyncEngine,
) -> None:
    """请求体带 operator/tenant_id → 422（静默忽略会让人以为身份是自己指定的）。"""
    event_id = await _seed_dead_letter(admin_engine)
    async with _client(roles=[_REPLAY_ROLE]) as client:
        resp = await client.post(
            f"/outbox/dead-letters/{event_id}/replay",
            json={
                "reason": "试图自报操作者",
                "operator": "ceo",
                "tenant_id": str(_OTHER_TENANT),
            },
        )
    assert resp.status_code == 422, resp.text
    assert await _audit_rows(admin_engine, event_id) == []


async def test_missing_reason_is_rejected(admin_engine: AsyncEngine) -> None:
    """没有理由的重放不可接受：审计行的意义就是记住「为什么」。"""
    event_id = await _seed_dead_letter(admin_engine)
    async with _client(roles=[_REPLAY_ROLE]) as client:
        resp = await client.post(
            f"/outbox/dead-letters/{event_id}/replay", json={"reason": ""}
        )
    assert resp.status_code == 422, resp.text
    assert await _audit_rows(admin_engine, event_id) == []


async def test_non_uuid_path_is_validation_error() -> None:
    async with _client(roles=[_REPLAY_ROLE]) as client:
        resp = await client.post(
            "/outbox/dead-letters/not-a-uuid/replay", json={"reason": "格式错误"}
        )
    assert resp.status_code == 422, resp.text


# --------------------------------------------------------------------------- #
# 并发：一次成功、一次冲突，审计恰好一行
# --------------------------------------------------------------------------- #
async def test_concurrent_replay_yields_one_success(
    admin_engine: AsyncEngine,
) -> None:
    """并发双发：一个 200、一个 409，审计恰好 1 行、事件仍只有 1 条。

    这是 D6-6 的真实验收。判定与写入之间的窗口由 ``FOR UPDATE`` 关闭：
    后到的事务在前者提交后重读到 ``dead_lettered_at IS NULL``，条件更新命中
    0 行，于是按冲突拒绝——而不是悄悄"成功"并写下第二条审计。
    """
    event_id = await _seed_dead_letter(admin_engine)
    count_before = await _event_count(admin_engine, TEST_TENANT_ID)
    async with _client(roles=[_REPLAY_ROLE]) as client:
        first, second = await asyncio.gather(
            client.post(
                f"/outbox/dead-letters/{event_id}/replay", json={"reason": "并发 A"}
            ),
            client.post(
                f"/outbox/dead-letters/{event_id}/replay", json={"reason": "并发 B"}
            ),
        )
    codes = sorted([first.status_code, second.status_code])
    assert codes == [200, 409], (first.text, second.text)

    assert len(await _audit_rows(admin_engine, event_id)) == 1
    assert await _event_count(admin_engine, TEST_TENANT_ID) == count_before
    assert event_id in await _claimable_ids(admin_engine)


# --------------------------------------------------------------------------- #
# 库层防线：审计表 append-only（授权层之外的第二道）
# --------------------------------------------------------------------------- #
async def test_audit_rows_are_append_only(admin_engine: AsyncEngine) -> None:
    """历史审计不可改写、清空：触发器直接拒绝（连超级用户也不例外）。

    只做授权层时，超级用户或将来某个误配的角色仍能改写历史；这条用例证明
    触发器层真的在，而不是"我们没给那个角色授权"。
    """
    event_id = await _seed_dead_letter(admin_engine)
    async with _client(roles=[_REPLAY_ROLE]) as client:
        resp = await client.post(
            f"/outbox/dead-letters/{event_id}/replay", json={"reason": "留痕"}
        )
    assert resp.status_code == 200, resp.text

    with pytest.raises(DBAPIError) as update_exc:
        async with admin_engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE outbox.dead_letter_replays SET reason = 'rewritten' "
                    "WHERE source_event_id = :eid"
                ),
                {"eid": event_id},
            )
    assert "append-only" in str(update_exc.value), str(update_exc.value)

    with pytest.raises(DBAPIError) as truncate_exc:
        async with admin_engine.begin() as conn:
            await conn.execute(text("TRUNCATE outbox.dead_letter_replays"))
    assert "append-only" in str(truncate_exc.value), str(truncate_exc.value)

    audits = await _audit_rows(admin_engine, event_id)
    assert len(audits) == 1
    assert audits[0]["reason"] == "留痕"
