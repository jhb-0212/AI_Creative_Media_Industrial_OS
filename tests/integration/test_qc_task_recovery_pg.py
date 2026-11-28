"""人工恢复入队的真实验收（C7-6：真实 PG 负向 + 并发）。

为什么必须打在真实 PostgreSQL 上
------------------------------
这条入口的六项判据里有四项只能在库里成立：

* C7-3 归属核验——依赖 RLS 与 ``production_planning_role`` 对
  ``project_planning.projects`` 的三列授权；用替身测等于测了个平行实现；
* C7-5 幂等——依赖 ``uq_qc_governance_qc_tasks_generation_task`` 这个**索引**，
  没有库就只有先查，而先查在并发下必然漏；
* C7-6 审计——要确认操作者/理由真的落进了 outbox 行，而不是只在日志里闪过；
* C7-2 席位——要确认真实令牌的角色/能力判定路径能拒掉不该进来的主体。

静态测试另行报告；本文件全部用例都实际运行（无 NOT RUN 项）。
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
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from apps.api.main import app
from packages.shared.database import build_migration_url
from tests.conftest import TEST_TENANT_ID, make_test_token
from tests.integration.planning_seed import seed_project

# 人工恢复的专项席位（契约 allowed_principals 去掉括号说明后的字面值）。
_RECOVER_ROLE = "运维恢复操作者"
_OTHER_TENANT = uuid.UUID("00000000-0000-0000-0000-0000000000c7")


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


def _client(*, roles: list[str], tenant_id: uuid.UUID = TEST_TENANT_ID) -> AsyncClient:
    """带真实令牌的客户端（走完整验签与授权链，不 mock Principal）。"""
    token = make_test_token(roles=roles, tenant_id=tenant_id)
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30.0,
    )


async def _seed_task(
    engine: AsyncEngine,
    sm: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID = TEST_TENANT_ID,
    status: str = "SUCCEEDED",
    asset_version_id: str | None = None,
    quality_profile_version: str | None = None,
) -> dict[str, str]:
    """造一个带真实项目归属的生成任务；默认是「登记成功且携带产物引用」。"""
    run = uuid.uuid4().hex[:10]
    project_id = f"rec-proj-{run}"
    task_id = f"rec-task-{run}"
    await seed_project(sm, project_id=project_id, tenant=tenant_id)
    now = datetime.now(UTC)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO production_planning.generation_tasks "
                "(entity_id, entity_type, schema_version, entity_version, status, "
                " owner_module, tenant_id, created_at, created_by, updated_at, "
                " generation_task_id, project_id, shot_id, shot_revision_id, "
                " production_route_ref, model_adapter_ref, constraint_snapshot_id, "
                " budget_ref, prompt_ref, lease_owner, lease_expires_at, "
                " attempt_count, asset_version_id, quality_profile_version) "
                "VALUES (:eid, 'GenerationTask', 205, 1, :status, "
                " 'production_planning', :tid, :now, 'recovery-seeder', :now, "
                " :task_id, :pid, 'shot-rec', 'rev-rec', 'route-rec', 'adapter-rec', "
                " 'snap-rec', 'budget-rec', 'inline:lighthouse', '', NULL, 0, :asset, :qp)"
            ),
            {
                "eid": f"rec-entity-{run}",
                "status": status,
                "tid": tenant_id,
                "now": now,
                "task_id": task_id,
                "pid": project_id,
                # 显式传空串表示「终态但没有产物引用」，不能被 or 兜底吃掉。
                "asset": (f"asset-{run}" if asset_version_id is None else asset_version_id),
                "qp": ("qp-v1" if quality_profile_version is None else quality_profile_version),
            },
        )
    return {"project_id": project_id, "generation_task_id": task_id}


async def _qc_rows(engine: AsyncEngine, generation_task_id: str) -> list[dict[str, Any]]:
    async with engine.connect() as conn:
        rows = (
            (
                await conn.execute(
                    text(
                        "SELECT entity_id, project_id, shot_id, asset_version_id, "
                        "quality_profile_version, created_by, status "
                        "FROM qc_governance.qc_tasks WHERE generation_task_id = :t"
                    ),
                    {"t": generation_task_id},
                )
            )
            .mappings()
            .all()
        )
        return [dict(r) for r in rows]


async def _queued_events(engine: AsyncEngine, generation_task_id: str) -> list[dict[str, Any]]:
    async with engine.connect() as conn:
        rows = (
            (
                await conn.execute(
                    text(
                        "SELECT payload FROM outbox.outbox_events "
                        "WHERE event_type='QC_TASK_QUEUED' "
                        "AND payload->>'generation_task_id' = :t "
                        "ORDER BY occurred_at"
                    ),
                    {"t": generation_task_id},
                )
            )
            .mappings()
            .all()
        )
        return [dict(r["payload"]) for r in rows]


async def test_recovery_derives_every_fact_from_database(
    admin_engine: AsyncEngine,
    admin_sm: async_sessionmaker[AsyncSession],
) -> None:
    """成功路径：三个「事实字段」全部来自任务行，操作者/理由落进事件载荷。"""
    seed = await _seed_task(admin_engine, admin_sm)
    async with _client(roles=[_RECOVER_ROLE]) as client:
        resp = await client.post(
            "/qc-tasks",
            json={
                "generation_task_id": seed["generation_task_id"],
                "project_id": seed["project_id"],
                "reason": "relay 死信导致漏入队，人工补录",
            },
        )
    assert resp.status_code == 201, resp.text
    body = resp.json()

    rows = await _qc_rows(admin_engine, seed["generation_task_id"])
    assert len(rows) == 1
    row = rows[0]
    assert row["entity_id"] == body["entity_id"]
    assert row["project_id"] == seed["project_id"]
    assert row["shot_id"] == "shot-rec"
    assert row["asset_version_id"].startswith("asset-")
    assert row["quality_profile_version"] == "qp-v1"
    assert row["created_by"] != "recovery-seeder"  # 操作者来自 Principal

    events = await _queued_events(admin_engine, seed["generation_task_id"])
    assert len(events) == 1
    assert events[0]["source"] == "MANUAL"
    assert events[0]["reason"] == "relay 死信导致漏入队，人工补录"
    assert events[0]["operator_id"] == "user_script"


async def test_body_cannot_smuggle_artifact_fields(
    admin_engine: AsyncEngine, admin_sm: async_sessionmaker[AsyncSession]
) -> None:
    """请求体里出现产物引用即 422：静默忽略会让人以为自己指定了产物。"""
    seed = await _seed_task(admin_engine, admin_sm)
    async with _client(roles=[_RECOVER_ROLE]) as client:
        resp = await client.post(
            "/qc-tasks",
            json={
                "generation_task_id": seed["generation_task_id"],
                "project_id": seed["project_id"],
                "reason": "试图自报产物引用",
                "asset_version_id": "asset-forged",
                "quality_profile_version": "qp-forged",
            },
        )
    assert resp.status_code == 422, resp.text
    assert await _qc_rows(admin_engine, seed["generation_task_id"]) == []


async def test_other_role_cannot_use_recovery_entry(
    admin_engine: AsyncEngine, admin_sm: async_sessionmaker[AsyncSession]
) -> None:
    """专项席位：普通 QC 角色即便持有能力也不得走人工入队。"""
    seed = await _seed_task(admin_engine, admin_sm)
    async with _client(roles=["QC 审核人"]) as client:
        resp = await client.post(
            "/qc-tasks",
            json={
                "generation_task_id": seed["generation_task_id"],
                "project_id": seed["project_id"],
                "reason": "越权尝试",
            },
        )
    assert resp.status_code == 403, resp.text
    assert await _qc_rows(admin_engine, seed["generation_task_id"]) == []


async def test_cross_tenant_task_is_invisible(
    admin_engine: AsyncEngine, admin_sm: async_sessionmaker[AsyncSession]
) -> None:
    """跨租户任务与不存在的任务同形（404）：不给探测位。"""
    foreign = await _seed_task(admin_engine, admin_sm, tenant_id=_OTHER_TENANT)
    async with _client(roles=[_RECOVER_ROLE]) as client:
        resp = await client.post(
            "/qc-tasks",
            json={
                "generation_task_id": foreign["generation_task_id"],
                "project_id": foreign["project_id"],
                "reason": "跨租户尝试",
            },
        )
    assert resp.status_code == 404, resp.text
    assert await _qc_rows(admin_engine, foreign["generation_task_id"]) == []


async def test_project_must_match_database_facts(
    admin_engine: AsyncEngine, admin_sm: async_sessionmaker[AsyncSession]
) -> None:
    """``project_id`` 是选择键，不是归属依据：与事实不符即 404。"""
    seed = await _seed_task(admin_engine, admin_sm)
    other = await _seed_task(admin_engine, admin_sm)
    async with _client(roles=[_RECOVER_ROLE]) as client:
        resp = await client.post(
            "/qc-tasks",
            json={
                "generation_task_id": seed["generation_task_id"],
                "project_id": other["project_id"],
                "reason": "拿错项目",
            },
        )
    assert resp.status_code == 404, resp.text
    assert await _qc_rows(admin_engine, seed["generation_task_id"]) == []


@pytest.mark.parametrize(
    "status", ["REQUESTED", "RUNNING", "AWAITING_ASSET_REGISTRATION", "FAILED"]
)
async def test_non_success_terminal_is_rejected(
    admin_engine: AsyncEngine, admin_sm: async_sessionmaker[AsyncSession], status: str
) -> None:
    """生命周期约束：只有登记成功终态可入队，其余 409。"""
    seed = await _seed_task(admin_engine, admin_sm, status=status)
    async with _client(roles=[_RECOVER_ROLE]) as client:
        resp = await client.post(
            "/qc-tasks",
            json={
                "generation_task_id": seed["generation_task_id"],
                "project_id": seed["project_id"],
                "reason": "非终态尝试",
            },
        )
    assert resp.status_code == 409, resp.text
    assert await _qc_rows(admin_engine, seed["generation_task_id"]) == []


async def test_success_without_artifact_ref_is_rejected(
    admin_engine: AsyncEngine,
    admin_sm: async_sessionmaker[AsyncSession],
) -> None:
    """终态成功但没有产物引用：拒绝，不猜一个值补上。"""
    seed = await _seed_task(admin_engine, admin_sm, asset_version_id="")
    async with _client(roles=[_RECOVER_ROLE]) as client:
        resp = await client.post(
            "/qc-tasks",
            json={
                "generation_task_id": seed["generation_task_id"],
                "project_id": seed["project_id"],
                "reason": "缺产物引用尝试",
            },
        )
    assert resp.status_code == 409, resp.text
    assert await _qc_rows(admin_engine, seed["generation_task_id"]) == []


async def test_second_recovery_conflicts_and_keeps_one_row(
    admin_engine: AsyncEngine,
    admin_sm: async_sessionmaker[AsyncSession],
) -> None:
    """重复补录 → 409；库里仍只有一条待检项、一条入队事件。"""
    seed = await _seed_task(admin_engine, admin_sm)
    payload = {
        "generation_task_id": seed["generation_task_id"],
        "project_id": seed["project_id"],
        "reason": "第一次补录",
    }
    async with _client(roles=[_RECOVER_ROLE]) as client:
        first = await client.post("/qc-tasks", json=payload)
        second = await client.post("/qc-tasks", json={**payload, "reason": "第二次补录"})
    assert first.status_code == 201, first.text
    assert second.status_code == 409, second.text
    assert len(await _qc_rows(admin_engine, seed["generation_task_id"])) == 1
    assert len(await _queued_events(admin_engine, seed["generation_task_id"])) == 1


async def test_concurrent_recovery_creates_exactly_one(
    admin_engine: AsyncEngine, admin_sm: async_sessionmaker[AsyncSession]
) -> None:
    """并发两次补录：一个 201、一个 409，库里恰好一行。

    这是 C7-5 的真实验收。唯一键只有在**冲突被翻译成业务码**之后才等于幂等：
    若沿用裸 flush，第二个请求会以 500 返回，调用方会重试，运维会以为系统坏了。
    """
    seed = await _seed_task(admin_engine, admin_sm)
    body = {
        "generation_task_id": seed["generation_task_id"],
        "project_id": seed["project_id"],
        "reason": "并发补录",
    }
    async with _client(roles=[_RECOVER_ROLE]) as client:
        first, second = await asyncio.gather(
            client.post("/qc-tasks", json=body),
            client.post("/qc-tasks", json=body),
        )
    codes = sorted([first.status_code, second.status_code])
    assert codes == [201, 409], (first.text, second.text)

    rows = await _qc_rows(admin_engine, seed["generation_task_id"])
    assert len(rows) == 1
    assert len(await _queued_events(admin_engine, seed["generation_task_id"])) == 1


async def test_manual_entry_does_not_bypass_unique_fact_with_relay(
    admin_engine: AsyncEngine,
    admin_sm: async_sessionmaker[AsyncSession],
) -> None:
    """人工与自动共用同一个唯一键：先自动入队后，人工补录必须 409。

    这条守的是「两个入口、一份事实」。若人工路径另建一张表或另一个键，
    下面这条断言就会变成「2 行、各自有效」——而那意味着同一份产物被检两次。
    """
    seed = await _seed_task(admin_engine, admin_sm)
    async with admin_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO qc_governance.qc_tasks "
                "(entity_id, entity_type, schema_version, entity_version, status, "
                " owner_module, tenant_id, created_at, created_by, updated_at, "
                " qc_task_id, project_id, shot_id, asset_version_id, "
                " generation_task_id, quality_profile_version, lease_owner, "
                " attempt_count, qc_report_id) "
                "VALUES (:eid, 'QCTask', 205, 1, 'QUEUED', 'qc-governance', :tid, "
                " :now, 'outbox-relay', :now, :eid, :pid, 'shot-rec', 'asset-relay', "
                " :task_id, 'qp-v1', '', 0, '')"
            ),
            {
                "eid": f"relay-qc-{uuid.uuid4().hex[:10]}",
                "tid": TEST_TENANT_ID,
                "now": datetime.now(UTC),
                "pid": seed["project_id"],
                "task_id": seed["generation_task_id"],
            },
        )
    async with _client(roles=[_RECOVER_ROLE]) as client:
        resp = await client.post(
            "/qc-tasks",
            json={
                "generation_task_id": seed["generation_task_id"],
                "project_id": seed["project_id"],
                "reason": "自动已入队后再补录",
            },
        )
    assert resp.status_code == 409, resp.text
    assert len(await _qc_rows(admin_engine, seed["generation_task_id"])) == 1


async def test_duplicate_generation_task_translates_to_port_error(
    admin_engine: AsyncEngine, admin_sm: async_sessionmaker[AsyncSession]
) -> None:
    """库层唯一键冲突必须翻译成 ``DuplicateEntityError``，且会话仍可用。

    为什么这条要打在**仓储边界**上：上面那条并发用例从 HTTP 打进去，两个请求
    可能只是恰好串行（先查就拦住了），证明不了「唯一键真的兜住了并发」。这里
    绕过先查直接撞索引，才是 C7-5 那句「唯一约束是并发兜底」的正面证据。

    顺带验证 SAVEPOINT：冲突之后同一会话还能继续用。若翻译靠把连接搞坏来实现，
    ``commit`` 会在更远的地方炸开，排障时看不出根因。
    """
    from packages.shared.port_errors import DuplicateEntityError
    from services.qc_governance.domain.qc_task import QCTask
    from services.qc_governance.repository.qc_task_repo import (
        SqlAlchemyQCTaskRepository,
    )

    seed = await _seed_task(admin_engine, admin_sm)

    def _task(entity_id: str) -> QCTask:
        return QCTask(
            entity_id=entity_id,
            created_by="ops-1",
            tenant_id=TEST_TENANT_ID,
            project_id=seed["project_id"],
            shot_id="shot-rec",
            asset_version_id="asset-1",
            generation_task_id=seed["generation_task_id"],
            quality_profile_version="qp-v1",
        )

    async with admin_sm() as session:
        await SqlAlchemyQCTaskRepository(session).add(_task(f"qc-first-{uuid.uuid4().hex[:8]}"))
        await session.commit()

    async with admin_sm() as session:
        with pytest.raises(DuplicateEntityError):
            await SqlAlchemyQCTaskRepository(session).add(
                _task(f"qc-second-{uuid.uuid4().hex[:8]}")
            )
        # SAVEPOINT 已回滚到冲突之前，同一会话仍能继续工作。
        assert (await session.execute(text("SELECT 1"))).scalar_one() == 1
