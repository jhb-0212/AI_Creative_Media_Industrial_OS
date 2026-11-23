"""R42 T0→T3 跨域故障注入验收（真实 PostgreSQL）。

对应 ADR-R42 §4.4「失败补偿」故障场景表。测的是**故障发生时系统的权威反应**，
不是「服务方法能被调用」：

- 对象存储失败（T1 未确认）→ 不登记，PREPARED 意图保留为失败证据；
- 产物已存储、登记事件未确认 → 从持久化 manifest 可重复投递，不重复建资产；
- AssetService 暂时不可用（基础设施异常）→ 不写拒绝回执、事件留给 Outbox 退避；
- 来源任务与资产内容不一致 → 可审计冲突，拒绝完成；
- 永久业务校验失败（同 id 不同内容）→ 正式拒绝回执 + 拒绝事件 → Generation FAILED；
- 多次重试仍失败 → 死信，不静默丢弃，数据保留可查。

故障注入点全部在**外部依赖边界**（monkeypatch 资产服务调用）或**持久化事实**
（篡改 outbox payload、预置冲突资产）；被验证的跨界事实仍由真实 PG 写入与回读。
绝不 mock 掉数据库本身——那等于把被测命题换成了一个平行实现。

写入类判据一律「期望 SQLSTATE/异常 + 回读影响行数」，不认「没抛异常即 PASS」：
RLS 会把越权写静默过滤成 0 行。

测试自造全部前置行，每个场景用新的 task id（回执 append-only，不能靠清理重跑）；
fixture 在 finally 复位在途任务，避免污染 CI 的 `downgrade -1`。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from packages.shared.database import (
    build_migration_url,
    create_asset_service_engine,
    create_production_planning_engine,
    create_sessionmaker,
)
from packages.shared.quality_profile import UNIFIED_HIGH_QUALITY
from packages.shared.rls import set_tenant_context
from packages.shared.storage_locator import build_locator
from services.asset_service.service.asset_service import AssetService
from services.production_planning.repository.sqlalchemy_repos import (
    SqlAlchemyGenerationTaskRepository,
)
from services.production_planning.service.generation_service import GenerationService
from services.project_planning.repository.sqlalchemy_outbox_writer import (
    SqlAlchemyOutboxWriter,
)
from workers.relay.asset_registration import (
    ReceiptEventError,
    build_asset_registration_handlers,
    parse_requested,
)
from workers.relay.consumer import EventHandler, OutboxEvent

_TENANT = UUID("00000000-0000-0000-0000-000000000043")
_DIGEST = "e" * 64
_LOCATOR = build_locator(_DIGEST, "png")
# 确定性资产 id：与 workers.relay.asset_registration._asset_entity_id 同公式。
# 测试独立重算，不从被测模块导入——否则「实现改了公式」与「测试跟着改」会一起
# 漂移，失去对「资产 ID 必须确定性派生」这条不变量的独立校验。
def _asset_id(task_id: str, digest: str = _DIGEST) -> str:
    return uuid5(
        NAMESPACE_URL,
        f"asset-registration:{_TENANT}:{task_id}:{digest}",
    ).hex


@pytest.fixture(scope="module")
async def admin_engine() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(build_migration_url(), pool_pre_ping=True)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture(scope="module")
async def prod_sm() -> AsyncIterator[object]:
    engine = create_production_planning_engine()
    try:
        yield create_sessionmaker(engine)
    finally:
        await engine.dispose()


@pytest.fixture(scope="module")
async def asset_sm() -> AsyncIterator[object]:
    engine = create_asset_service_engine()
    try:
        yield create_sessionmaker(engine)
    finally:
        await engine.dispose()


async def _seed(admin_engine: AsyncEngine, *, status: str = "RUNNING") -> dict[str, object]:
    """造一个带租约的在途任务；每个场景用新 id，避免 append-only 冲突。"""
    run = uuid4()
    project_id = f"fi-proj-{run}"
    task_id = f"fi-task-{run}"
    task_entity = f"fi-entity-{run}"
    now = datetime.now(UTC)
    async with admin_engine.begin() as conn:
        await conn.execute(
            sa.text(
                "INSERT INTO project_planning.projects "
                "(entity_id, entity_type, schema_version, entity_version, status, "
                " owner_module, tenant_id, created_at, created_by, updated_at, "
                " project_name, project_tier, organization_id) "
                "VALUES (:pid, 'Project', 205, 1, 'DRAFT', 'project_planning', "
                " :tid, :now, 'fault-injector', :now, '故障注入', 'B', NULL)"
            ),
            {"pid": project_id, "tid": _TENANT, "now": now},
        )
        await conn.execute(
            sa.text(
                "INSERT INTO production_planning.generation_tasks "
                "(entity_id, entity_type, schema_version, entity_version, status, "
                " owner_module, tenant_id, created_at, created_by, updated_at, "
                " generation_task_id, project_id, shot_id, shot_revision_id, "
                " production_route_ref, model_adapter_ref, constraint_snapshot_id, "
                " budget_ref, prompt_ref, lease_owner, lease_expires_at, attempt_count, "
                " asset_version_id, quality_profile_version) "
                "VALUES (:eid, 'GenerationTask', 205, 1, :status, "
                " 'production_planning', :tid, :now, 'fault-injector', :now, "
                " :task_id, :pid, 'shot-1', 'rev-1', 'route-1', 'adapter-1', 'snap-1', "
                " 'budget-1', 'inline:lighthouse', 'worker-fi', :expiry, 1, '', :qp)"
            ),
            {
                "eid": task_entity,
                "tid": _TENANT,
                "now": now,
                "status": status,
                "task_id": task_id,
                "pid": project_id,
                "qp": UNIFIED_HIGH_QUALITY,
                "expiry": now.replace(year=now.year + 1),
            },
        )
    return {
        "project_id": project_id,
        "task_id": task_id,
        "task_entity": task_entity,
        "attempt_id": f"attempt-{run}",
    }


@pytest.fixture
async def seed(admin_engine: AsyncEngine) -> AsyncIterator[dict[str, object]]:
    data = await _seed(admin_engine)
    try:
        yield data
    finally:
        # 失败路径也必须复位在途状态：CI 在 pytest 之后跑 `downgrade -1`，
        # 旧 CHECK 不接受 AWAITING_ASSET_REGISTRATION。
        async with admin_engine.begin() as conn:
            await conn.execute(
                sa.text(
                    "UPDATE production_planning.generation_tasks "
                    "SET status = 'SUCCEEDED' "
                    "WHERE generation_task_id = :task "
                    "AND status = 'AWAITING_ASSET_REGISTRATION'"
                ),
                {"task": data["task_id"]},
            )


async def _count(admin_engine: AsyncEngine, sql: str, params: dict[str, object]) -> int:
    async with admin_engine.connect() as conn:
        return int(await conn.scalar(sa.text(sql), params) or 0)


async def _outbox(
    admin_engine: AsyncEngine, event_type: str, task_id: str
) -> OutboxEvent:
    async with admin_engine.connect() as conn:
        row = (
            await conn.execute(
                sa.text(
                    "SELECT event_id, aggregate_type, aggregate_id, tenant_id, "
                    "event_type, payload, occurred_at FROM outbox.outbox_events "
                    "WHERE event_type = :etype AND tenant_id = :tid "
                    "AND payload->>'generation_task_id' = :task "
                    "ORDER BY occurred_at DESC LIMIT 1"
                ),
                {"etype": event_type, "tid": _TENANT, "task": task_id},
            )
        ).one()
    return OutboxEvent(
        event_id=row.event_id,
        aggregate_type=row.aggregate_type,
        aggregate_id=row.aggregate_id,
        tenant_id=row.tenant_id,
        event_type=row.event_type,
        payload=dict(row.payload),
        occurred_at=row.occurred_at,
    )


async def _prepare_and_confirm(
    prod_sm: object, seed: dict[str, object]
) -> None:
    """T0 + T1：落 PREPARED 再确认 STORED_VERIFIED，发登记请求事件。"""
    task_entity = str(seed["task_entity"])
    attempt_id = str(seed["attempt_id"])
    async with prod_sm() as session:  # type: ignore[operator]
        await set_tenant_context(session, _TENANT)
        service = GenerationService(
            SqlAlchemyGenerationTaskRepository(session), SqlAlchemyOutboxWriter(session)
        )
        await service.prepare_artifact(
            task_id=task_entity,
            worker_id="worker-fi",
            locator=_LOCATOR,
            sha256=_DIGEST,
            attempt_id=attempt_id,
        )
        await session.commit()
    async with prod_sm() as session:  # type: ignore[operator]
        await set_tenant_context(session, _TENANT)
        service = GenerationService(
            SqlAlchemyGenerationTaskRepository(session), SqlAlchemyOutboxWriter(session)
        )
        await service.confirm_artifact(
            task_id=task_entity,
            worker_id="worker-fi",
            attempt_id=attempt_id,
            locator=_LOCATOR,
            sha256=_DIGEST,
            media_type="image/png",
        )
        await session.commit()


def _handlers(prod_sm: object, asset_sm: object) -> dict[str, EventHandler]:
    return build_asset_registration_handlers(
        production_planning_sessionmaker=prod_sm,
        asset_sessionmaker=asset_sm,
    )


async def test_storage_failure_keeps_prepared_evidence_no_registration(
    admin_engine: AsyncEngine, prod_sm: object, seed: dict[str, object]
) -> None:
    """对象存储临时失败：T0 意图保留为失败证据，不得登记、不得发登记请求。"""
    task_id = str(seed["task_id"])
    task_entity = str(seed["task_entity"])
    async with prod_sm() as session:  # type: ignore[operator]
        await set_tenant_context(session, _TENANT)
        service = GenerationService(
            SqlAlchemyGenerationTaskRepository(session), SqlAlchemyOutboxWriter(session)
        )
        await service.prepare_artifact(
            task_id=task_entity,
            worker_id="worker-fi",
            locator=_LOCATOR,
            sha256=_DIGEST,
            attempt_id=str(seed["attempt_id"]),
        )
        await session.commit()

    # T1 未确认（对象写入失败）：意图停在 PREPARED，状态仍在 RUNNING。
    assert await _count(
        admin_engine,
        "SELECT count(*) FROM production_planning.artifact_manifests "
        "WHERE status='PREPARED' AND generation_task_id=:task",
        {"task": task_id},
    ) == 1
    assert await _count(
        admin_engine,
        "SELECT count(*) FROM production_planning.generation_tasks "
        "WHERE entity_id=:eid AND status='RUNNING'",
        {"eid": task_entity},
    ) == 1
    # 没有登记请求、没有回执、没有资产——失败没有被误报成登记成功。
    assert await _count(
        admin_engine,
        "SELECT count(*) FROM outbox.outbox_events "
        "WHERE event_type='ASSET_REGISTRATION_REQUESTED' "
        "AND payload->>'generation_task_id' = :task",
        {"task": task_id},
    ) == 0
    assert await _count(
        admin_engine,
        "SELECT count(*) FROM asset_service.asset_registration_receipts "
        "WHERE generation_task_id=:task",
        {"task": task_id},
    ) == 0


async def test_tampered_request_payload_is_auditable_conflict(
    admin_engine: AsyncEngine,
    prod_sm: object,
    asset_sm: object,
    seed: dict[str, object],
) -> None:
    """来源任务与资产内容不一致：事件载荷被篡改，T2 拒绝完成形成可审计冲突。"""
    task_id = str(seed["task_id"])
    await _prepare_and_confirm(prod_sm, seed)
    requested = await _outbox(admin_engine, "ASSET_REGISTRATION_REQUESTED", task_id)
    # 篡改 sha256：与持久化 manifest 矛盾（模拟上游被污染/中间人改载荷）。
    tampered = OutboxEvent(
        event_id=requested.event_id,
        aggregate_type=requested.aggregate_type,
        aggregate_id=requested.aggregate_id,
        tenant_id=requested.tenant_id,
        event_type=requested.event_type,
        payload={**requested.payload, "sha256": "b" * 64},
        occurred_at=requested.occurred_at,
    )
    handlers = _handlers(prod_sm, asset_sm)
    with pytest.raises(ReceiptEventError, match="不一致"):
        await handlers["ASSET_REGISTRATION_REQUESTED"](tampered)

    # 拒绝后没有任何跨域写入：无资产、无回执、任务仍停在等待态。
    assert await _count(
        admin_engine,
        "SELECT count(*) FROM asset_service.asset_versions WHERE entity_id=:aid",
        {"aid": _asset_id(task_id)},
    ) == 0
    assert await _count(
        admin_engine,
        "SELECT count(*) FROM asset_service.asset_registration_receipts "
        "WHERE generation_task_id=:task",
        {"task": task_id},
    ) == 0
    assert await _count(
        admin_engine,
        "SELECT count(*) FROM production_planning.generation_tasks "
        "WHERE generation_task_id=:task AND status='AWAITING_ASSET_REGISTRATION'",
        {"task": task_id},
    ) == 1


async def test_transient_asset_service_failure_has_no_reject_receipt(
    admin_engine: AsyncEngine,
    prod_sm: object,
    asset_sm: object,
    seed: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AssetService 暂时不可用（基础设施异常）：不写拒绝回执，故障交给上层退避。

    关键：只有永久业务冲突才写 REJECTED 回执。一次短暂依赖抖动若被写成拒绝，
    会把可恢复任务永久判死。这里注入一个非 DomainError 异常，断言**没有**回执、
    **没有**终态事件——事件保持未投递，由 relay 的退避机制处理。
    """
    task_id = str(seed["task_id"])
    await _prepare_and_confirm(prod_sm, seed)

    async def flaky(self: AssetService, **kwargs: object) -> object:
        raise ConnectionError("simulated: asset service temporarily unavailable")

    monkeypatch.setattr(AssetService, "create_version", flaky)
    try:
        handlers = _handlers(prod_sm, asset_sm)
        requested = await _outbox(admin_engine, "ASSET_REGISTRATION_REQUESTED", task_id)
        # parse_requested 仍可解析（故障在资产服务，不在载荷）。
        assert parse_requested(requested).generation_task_id == task_id
        with pytest.raises(ConnectionError, match="temporarily unavailable"):
            await handlers["ASSET_REGISTRATION_REQUESTED"](requested)
    finally:
        monkeypatch.undo()

    # 基础设施故障没有留下任何「永久拒绝」事实。
    assert await _count(
        admin_engine,
        "SELECT count(*) FROM asset_service.asset_registration_receipts "
        "WHERE generation_task_id=:task",
        {"task": task_id},
    ) == 0
    assert await _count(
        admin_engine,
        "SELECT count(*) FROM asset_service.asset_versions WHERE entity_id=:aid",
        {"aid": _asset_id(task_id)},
    ) == 0
    # 任务仍是等待态：没有被错误推进到 FAILED。
    assert await _count(
        admin_engine,
        "SELECT count(*) FROM production_planning.generation_tasks "
        "WHERE generation_task_id=:task AND status='AWAITING_ASSET_REGISTRATION'",
        {"task": task_id},
    ) == 1


async def test_permanent_conflict_writes_reject_receipt_and_fails_task(
    admin_engine: AsyncEngine,
    prod_sm: object,
    asset_sm: object,
    seed: dict[str, object],
) -> None:
    """永久业务校验失败（同 id 不同内容）：拒绝回执 + 拒绝事件 + Generation FAILED。

    预置一个与确定性资产 id 相同、但 sha256 不同的资产版本，使 create_version
    真实抛出 IDEMPOTENCY_CONFLICT（DomainError）——这是「同 id 不同内容的覆盖请求」
    的真实触发，不是 mock。T2 必须写正式拒绝回执，T3 消费后任务转 FAILED。
    """
    task_id = str(seed["task_id"])
    task_entity = str(seed["task_entity"])
    project_id = str(seed["project_id"])
    aid = _asset_id(task_id)
    other_digest = "a" * 64
    now = datetime.now(UTC)

    # 预置冲突资产：相同确定性 id，不同内容。storage_ref 仍须是自洽定位符。
    async with admin_engine.begin() as conn:
        await conn.execute(
            sa.text(
                "INSERT INTO asset_service.asset_versions "
                "(entity_id, entity_type, schema_version, entity_version, status, "
                " owner_module, tenant_id, created_at, created_by, updated_at, "
                " project_id, asset_type, sha256, storage_ref, "
                " source_asset_version_ids, version) "
                "VALUES (:id, 'AssetVersion', 1, 1, 'APPROVED', 'asset_service', "
                " :tid, :now, 'fault-injector', :now, :pid, 'PNG', :sha, "
                " :locator, '[]'::jsonb, 'qp-fi')"
            ),
            {
                "id": aid,
                "tid": _TENANT,
                "now": now,
                "pid": project_id,
                "sha": other_digest,
                "locator": build_locator(other_digest, "png"),
            },
        )

    await _prepare_and_confirm(prod_sm, seed)
    handlers = _handlers(prod_sm, asset_sm)
    requested = await _outbox(admin_engine, "ASSET_REGISTRATION_REQUESTED", task_id)
    # 永久冲突被 handler 捕获为拒绝回执，不向上抛（与基础设施故障相反）。
    await handlers["ASSET_REGISTRATION_REQUESTED"](requested)

    assert await _count(
        admin_engine,
        "SELECT count(*) FROM asset_service.asset_registration_receipts "
        "WHERE generation_task_id=:task AND status='REJECTED' "
        "AND message LIKE '%已存在且内容不同%'",
        {"task": task_id},
    ) == 1
    assert await _count(
        admin_engine,
        "SELECT count(*) FROM outbox.outbox_events "
        "WHERE event_type='ASSET_REGISTRATION_REJECTED' "
        "AND payload->>'generation_task_id' = :task",
        {"task": task_id},
    ) == 1
    # 拒绝回执的 asset_version_id 必须为空（不能指向一个并不存在的新资产）。
    assert await _count(
        admin_engine,
        "SELECT count(*) FROM asset_service.asset_registration_receipts "
        "WHERE generation_task_id=:task AND (asset_version_id IS NULL OR asset_version_id='')",
        {"task": task_id},
    ) == 1

    # T3 消费拒绝事件 → Generation 转 FAILED。
    rejected = await _outbox(admin_engine, "ASSET_REGISTRATION_REJECTED", task_id)
    await handlers["ASSET_REGISTRATION_REJECTED"](rejected)
    assert await _count(
        admin_engine,
        "SELECT count(*) FROM production_planning.generation_tasks "
        "WHERE entity_id=:eid AND status='FAILED'",
        {"eid": task_entity},
    ) == 1
    assert await _count(
        admin_engine,
        "SELECT count(*) FROM outbox.outbox_events WHERE event_type='GENERATION_FAILED' "
        "AND payload->>'generation_task_id' = :task",
        {"task": task_id},
    ) == 1
    # 成功事件绝不能被写出。
    assert await _count(
        admin_engine,
        "SELECT count(*) FROM outbox.outbox_events WHERE event_type='GENERATION_SUCCEEDED' "
        "AND payload->>'generation_task_id' = :task",
        {"task": task_id},
    ) == 0


async def test_receipt_result_event_contradiction_is_rejected(
    admin_engine: AsyncEngine,
    prod_sm: object,
    asset_sm: object,
    seed: dict[str, object],
) -> None:
    """登记结果事件与回执矛盾：T3 拒绝推进终态，防止伪造事件改任务状态。"""
    task_id = str(seed["task_id"])
    await _prepare_and_confirm(prod_sm, seed)
    handlers = _handlers(prod_sm, asset_sm)
    requested = await _outbox(admin_engine, "ASSET_REGISTRATION_REQUESTED", task_id)
    await handlers["ASSET_REGISTRATION_REQUESTED"](requested)

    completed = await _outbox(admin_engine, "ASSET_REGISTRATION_COMPLETED", task_id)
    # 篡改资产 ID：与不可变回执里的资产 ID 矛盾。
    forged = OutboxEvent(
        event_id=completed.event_id,
        aggregate_type=completed.aggregate_type,
        aggregate_id=completed.aggregate_id,
        tenant_id=completed.tenant_id,
        event_type=completed.event_type,
        payload={**completed.payload, "asset_version_id": "forged-asset-id"},
        occurred_at=completed.occurred_at,
    )
    with pytest.raises(ReceiptEventError, match="不一致"):
        await handlers["ASSET_REGISTRATION_COMPLETED"](forged)

    # 任务仍停在等待态，没有被伪造事件推进。
    assert await _count(
        admin_engine,
        "SELECT count(*) FROM production_planning.generation_tasks "
        "WHERE generation_task_id=:task AND status='AWAITING_ASSET_REGISTRATION'",
        {"task": task_id},
    ) == 1
    assert await _count(
        admin_engine,
        "SELECT count(*) FROM outbox.outbox_events WHERE event_type='GENERATION_SUCCEEDED' "
        "AND payload->>'generation_task_id' = :task",
        {"task": task_id},
    ) == 0


async def test_retry_recovers_after_transient_failure_without_second_asset(
    admin_engine: AsyncEngine,
    prod_sm: object,
    asset_sm: object,
    seed: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """登记事件重投：首次依赖抖动失败、重试成功后只建一个资产与一条回执。

    证明「登记成功但 Generation 未完成 → 由回执 + Outbox 重放完成关联」以及
    「事件重复投递按回执幂等」——第一次失败没写回执，第二次成功写一条，
    第三次重投复用既有回执，不新建第二个资产。
    """
    task_id = str(seed["task_id"])
    task_entity = str(seed["task_entity"])
    await _prepare_and_confirm(prod_sm, seed)
    requested = await _outbox(admin_engine, "ASSET_REGISTRATION_REQUESTED", task_id)
    handlers = _handlers(prod_sm, asset_sm)

    original = AssetService.create_version
    state = {"fail": True}

    async def flaky_once(self: AssetService, **kwargs: Any) -> Any:
        if state["fail"]:
            state["fail"] = False
            raise ConnectionError("simulated: one-off dependency blip")
        return await original(self, **kwargs)

    monkeypatch.setattr(AssetService, "create_version", flaky_once)
    try:
        with pytest.raises(ConnectionError):
            await handlers["ASSET_REGISTRATION_REQUESTED"](requested)
        # 失败落空：没有回执。
        assert await _count(
            admin_engine,
            "SELECT count(*) FROM asset_service.asset_registration_receipts "
            "WHERE generation_task_id=:task",
            {"task": task_id},
        ) == 0
        # 重投成功。
        await handlers["ASSET_REGISTRATION_REQUESTED"](requested)
        # 再次重投：幂等，不新建。
        await handlers["ASSET_REGISTRATION_REQUESTED"](requested)
    finally:
        monkeypatch.undo()

    assert await _count(
        admin_engine,
        "SELECT count(*) FROM asset_service.asset_registration_receipts "
        "WHERE generation_task_id=:task AND status='COMPLETED'",
        {"task": task_id},
    ) == 1
    assert await _count(
        admin_engine,
        "SELECT count(*) FROM asset_service.asset_versions WHERE entity_id=:aid",
        {"aid": _asset_id(task_id)},
    ) == 1

    # 消费完成事件 → 终态资产 ID 来自回执。
    completed = await _outbox(admin_engine, "ASSET_REGISTRATION_COMPLETED", task_id)
    await handlers["ASSET_REGISTRATION_COMPLETED"](completed)
    await handlers["ASSET_REGISTRATION_COMPLETED"](completed)
    assert await _count(
        admin_engine,
        "SELECT count(*) FROM production_planning.generation_tasks "
        "WHERE entity_id=:eid AND status='SUCCEEDED' AND asset_version_id=:aid",
        {"eid": task_entity, "aid": _asset_id(task_id)},
    ) == 1
    assert await _count(
        admin_engine,
        "SELECT count(*) FROM outbox.outbox_events WHERE event_type='GENERATION_SUCCEEDED' "
        "AND payload->>'generation_task_id' = :task",
        {"task": task_id},
    ) == 1


async def test_repeated_failure_reaches_dead_letter_and_keeps_event(
    admin_engine: AsyncEngine,
    prod_sm: object,
    asset_sm: object,
    seed: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    relay_sessionmaker: object,
) -> None:
    """多次重试仍失败 → 事件进入死信、不静默丢弃、数据保留可查。

    用真实 OutboxRelay + max_attempts=1 跑一轮，注入持续的基础设施异常使
    T2 handler 一直失败。断言事件被打死信标记、attempts 累加、未 ack，
    且事件行仍可查（投递放弃本身是可审计事实）。
    """
    from workers.relay.consumer import OutboxRelay

    task_id = str(seed["task_id"])
    await _prepare_and_confirm(prod_sm, seed)

    async def always_down(self: AssetService, **kwargs: object) -> object:
        raise ConnectionError("simulated: asset service permanently down")

    monkeypatch.setattr(AssetService, "create_version", always_down)
    relay = OutboxRelay(
        relay_sessionmaker,  # type: ignore[arg-type]
        _handlers(prod_sm, asset_sm),
        max_attempts=1,
    )
    try:
        stats = await relay.drain_tenant(_TENANT)
    finally:
        monkeypatch.undo()

    # 至少一个事件失败并进入死信（本租户可能有其它历史未投递事件，故用 >=）。
    assert stats.failed >= 1
    assert stats.dead_lettered >= 1
    assert await _count(
        admin_engine,
        "SELECT count(*) FROM outbox.outbox_events "
        "WHERE event_type='ASSET_REGISTRATION_REQUESTED' "
        "AND payload->>'generation_task_id' = :task "
        "AND dead_lettered_at IS NOT NULL AND published_at IS NULL",
        {"task": task_id},
    ) == 1
    # 死信不等于「登记成功」：没有回执、没有资产、任务仍等待。
    assert await _count(
        admin_engine,
        "SELECT count(*) FROM asset_service.asset_registration_receipts "
        "WHERE generation_task_id=:task",
        {"task": task_id},
    ) == 0
    assert await _count(
        admin_engine,
        "SELECT count(*) FROM production_planning.generation_tasks "
        "WHERE generation_task_id=:task AND status='AWAITING_ASSET_REGISTRATION'",
        {"task": task_id},
    ) == 1
