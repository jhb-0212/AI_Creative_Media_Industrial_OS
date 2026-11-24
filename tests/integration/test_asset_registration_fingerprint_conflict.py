"""R42 T2 幂等冲突真实 PG 验收（PR-F）。

证明「同幂等键、不同语义」不会被洗成正常重复：
- 同一 task 先以 attempt-1 正常登记（回执带指纹 A，发 COMPLETED）；
- 再为同一 task 建 attempt-2 的不同产物意图（不同 sha/locator），构造
  REQUESTED 事件；其生成域核验全部通过，但指纹与回执不同；
- 断言：不建第二资产、不改写原回执、发出 ASSET_REGISTRATION_CONFLICT 事件；
- 同指纹重复投递仍幂等（不产生第二条冲突事件）。

attempt-2 的 manifest 用 prepare_artifact 建立（该方法不检查 task 状态），
再经 repository.save_manifest(mark_stored()) 推到 STORED_VERIFIED——这是
T1 确认的领域事实，不手工 SQL 改状态。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from packages.shared.database import (
    build_migration_url,
    create_asset_service_engine,
    create_production_planning_engine,
    create_sessionmaker,
)
from packages.shared.event_payloads import stamp_event_payload
from packages.shared.port_errors import DuplicateEntityError
from packages.shared.rls import set_tenant_context
from packages.shared.storage_locator import build_locator
from services.asset_service.domain.asset_registration_receipt import (
    AssetRegistrationReceipt,
)
from services.asset_service.domain.enums import AssetRegistrationReceiptStatus
from services.asset_service.repository.sqlalchemy_receipt_repo import (
    SqlAlchemyAssetRegistrationReceiptRepository,
)
from services.production_planning.repository.sqlalchemy_repos import (
    SqlAlchemyGenerationTaskRepository,
)
from services.production_planning.service.generation_service import GenerationService
from services.project_planning.repository.sqlalchemy_outbox_writer import (
    SqlAlchemyOutboxWriter,
)

# 复用 saga e2e 的真实 seed（Route→Task→RUNNING+lease），避免重复一套夹具。
from tests.integration.test_asset_registration_saga_e2e import _seed
from workers.relay.asset_registration import build_asset_registration_handlers
from workers.relay.consumer import OutboxEvent

_TENANT = UUID("00000000-0000-0000-0000-000000000041")


def _digests_for(run: object) -> tuple[str, str]:
    """每次测试用唯一摘要：资产是 append-only，固定摘要会在重跑时撞既存行。"""
    seed = str(run).replace("-", "")[:64].ljust(64, "0")
    d1 = ("a" + seed[1:])[:64]
    d2 = ("b" + seed[1:])[:64]
    return d1, d2


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


@pytest.fixture
async def seeded(admin_engine: AsyncEngine, prod_sm: object) -> AsyncIterator[dict[str, object]]:
    data = await _seed(admin_engine, prod_sm)
    try:
        yield data
    finally:
        # append-only 回执/资产不可删；把 task 推回终态，供 CI downgrade -1。
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


def _requested_event(task_id: str, project_id: str, attempt_id: str,
                     digest: str, locator: str) -> OutboxEvent:
    return OutboxEvent(
        event_id=uuid4(),
        aggregate_type="GenerationTask",
        aggregate_id="",
        tenant_id=_TENANT,
        event_type="ASSET_REGISTRATION_REQUESTED",
        payload=stamp_event_payload(
            {
                "generation_task_id": task_id,
                "project_id": project_id,
                "tenant_id": str(_TENANT),
                "locator": locator,
                "sha256": digest,
                "attempt_id": attempt_id,
                "media_type": "image/png",
                "quality_profile_version": "unified-high-quality-v1",
                "worker_id": "worker-e2e",
                "from_status": "RUNNING",
                "to_status": "AWAITING_ASSET_REGISTRATION",
                "transition_id": f"tr-{attempt_id}",
            }
        ),
        occurred_at=datetime.now(UTC),
    )


async def test_same_task_different_attempt_records_conflict(
    admin_engine: AsyncEngine, prod_sm: object, asset_sm: object,
    seeded: dict[str, object],
) -> None:
    task_id = str(seeded["task_id"])
    task_entity = str(seeded["task_entity"])
    project_id = str(seeded["project_id"])
    attempt_1 = f"attempt-fp1-{seeded['run']}"
    attempt_2 = f"attempt-fp2-{seeded['run']}"
    digest_1, digest_2 = _digests_for(seeded["run"])
    locator_1 = build_locator(digest_1, "png")
    locator_2 = build_locator(digest_2, "png")

    # attempt-1：prepare + confirm → AWAITING + REQUESTED 事件。
    async with prod_sm() as session:  # type: ignore[operator]
        await set_tenant_context(session, _TENANT)
        svc = GenerationService(
            SqlAlchemyGenerationTaskRepository(session), SqlAlchemyOutboxWriter(session)
        )
        await svc.prepare_artifact(
            task_id=task_entity, worker_id="worker-e2e",
            locator=locator_1, sha256=digest_1, media_type="image/png",
            attempt_id=attempt_1,
        )
        await session.commit()
    async with prod_sm() as session:  # type: ignore[operator]
        await set_tenant_context(session, _TENANT)
        svc = GenerationService(
            SqlAlchemyGenerationTaskRepository(session), SqlAlchemyOutboxWriter(session)
        )
        await svc.confirm_artifact(
            task_id=task_entity, worker_id="worker-e2e", attempt_id=attempt_1,
            locator=locator_1, sha256=digest_1, media_type="image/png",
        )
        await session.commit()

    handlers = build_asset_registration_handlers(
        production_planning_sessionmaker=prod_sm, asset_sessionmaker=asset_sm
    )
    req1 = _requested_event(task_id, project_id, attempt_1, digest_1, locator_1)
    await handlers["ASSET_REGISTRATION_REQUESTED"](req1)

    # 首次登记成功：一条回执、带指纹、一个资产。
    assert await _count(
        admin_engine,
        "SELECT count(*) FROM asset_service.asset_registration_receipts "
        "WHERE generation_task_id=:task AND registration_fingerprint IS NOT NULL",
        {"task": task_id},
    ) == 1
    assert await _count(
        admin_engine,
        "SELECT count(*) FROM asset_service.asset_versions WHERE sha256=:d",
        {"d": digest_1},
    ) == 1

    # attempt-2：同 task 不同产物的 STORED_VERIFIED 意图。
    # 正常入口在 confirm 后 task 转 AWAITING，prepare 会被租约守卫拒绝——即
    # 业务上不应有第二个合法 attempt。这里用 admin 连接直接注入该 manifest，
    # 正是模拟指纹要防的威胁：绕过正常入口的注入/重放产生「同键不同语义」请求。
    # handler 的核验只查 manifest 状态与 locator/sha，不要求 task 还能 prepare，
    # 因此这条注入会走到指纹判定——测的正是那道防线的真实反应。
    async with admin_engine.begin() as conn:
        await conn.execute(
            sa.text(
                "INSERT INTO production_planning.artifact_manifests "
                "(entity_id, entity_type, schema_version, entity_version, status, "
                "owner_module, tenant_id, created_at, created_by, updated_at, "
                "generation_task_id, project_id, locator, sha256, media_type, attempt_id) "
                "VALUES (:eid, 'ArtifactManifest', 1, 1, 'STORED_VERIFIED', "
                "'production-planning', :tid, :now, 'injected', :now, "
                ":task, :proj, :loc, :sha, 'image/png', :attempt)"
            ),
            {
                "eid": f"injected-{uuid4()}",
                "tid": _TENANT,
                "now": datetime.now(UTC),
                "task": task_id,
                "proj": project_id,
                "loc": locator_2,
                "sha": digest_2,
                "attempt": attempt_2,
            },
        )

    conflict_sql = (
        "SELECT count(*) FROM outbox.outbox_events "
        "WHERE event_type='ASSET_REGISTRATION_CONFLICT' "
        "AND payload->>'generation_task_id' = :task "
        "AND payload ? 'expected_fingerprint' AND payload ? 'observed_fingerprint'"
    )
    before = await _count(admin_engine, conflict_sql, {"task": task_id})

    # 同 task、attempt-2 的 REQUESTED：核验全通过，但指纹不同 → 冲突。
    req2 = _requested_event(task_id, project_id, attempt_2, digest_2, locator_2)
    await handlers["ASSET_REGISTRATION_REQUESTED"](req2)
    after_first = await _count(admin_engine, conflict_sql, {"task": task_id})
    # 重投同一冲突请求：不产生第二条冲突证据（写入幂等）。
    await handlers["ASSET_REGISTRATION_REQUESTED"](req2)
    after_replay = await _count(admin_engine, conflict_sql, {"task": task_id})

    # 不建第二资产、不改写原回执。
    assert await _count(
        admin_engine,
        "SELECT count(*) FROM asset_service.asset_versions WHERE sha256=:d",
        {"d": digest_2},
    ) == 0
    assert await _count(
        admin_engine,
        "SELECT count(*) FROM asset_service.asset_registration_receipts "
        "WHERE generation_task_id=:task",
        {"task": task_id},
    ) == 1
    # 冲突事件：首次新增一条，重投不再增加（幂等），payload 含双指纹。
    assert after_first == before + 1, f"首次冲突应新增一条：{before}->{after_first}"
    assert after_replay == after_first, f"重投不应新增冲突证据：{after_first}->{after_replay}"

    # 冲突事件由专用 no-op handler 正常消费（不动业务状态、不抬高积压语义）。
    conflict_event = None
    async with admin_engine.connect() as conn:
        row = (await conn.execute(
            sa.text(
                "SELECT event_id, aggregate_type, aggregate_id, tenant_id, event_type, "
                "payload, occurred_at FROM outbox.outbox_events "
                "WHERE event_type='ASSET_REGISTRATION_CONFLICT' "
                "AND payload->>'generation_task_id' = :task LIMIT 1"
            ),
            {"task": task_id},
        )).one()
        conflict_event = OutboxEvent(
            event_id=row.event_id, aggregate_type=row.aggregate_type,
            aggregate_id=row.aggregate_id, tenant_id=row.tenant_id,
            event_type=row.event_type, payload=dict(row.payload),
            occurred_at=row.occurred_at,
        )
    await handlers["ASSET_REGISTRATION_CONFLICT"](conflict_event)  # 不抛异常即 no-op
    assert await _count(
        admin_engine,
        "SELECT count(*) FROM production_planning.generation_tasks "
        "WHERE generation_task_id=:task AND status='AWAITING_ASSET_REGISTRATION'",
        {"task": task_id},
    ) == 1  # 冲突不改任务状态


async def test_receipt_unique_conflict_is_recoverable_for_handler_reread(
    asset_sm: object, seeded: dict[str, object],
) -> None:
    """G-3 的仓储契约：唯一键冲突抛可回读的 DuplicateEntityError，session 不 aborted。

    handler 的并发收敛分支依赖此契约：两个 Relay 都判空后插入，后写者撞
    ``uq_asset_registration_receipts_tenant_task``，必须能回读既有回执转指纹比对，
    而不是把 23505 冒泡成死信。这里用真实 PG + 真实 repo 锁住这个前提。
    """
    task_id = str(seeded["task_id"])
    project_id = str(seeded["project_id"])

    def make_receipt(entity_suffix: str, fingerprint: str) -> AssetRegistrationReceipt:
        return AssetRegistrationReceipt(
            entity_id=f"prf-reread-{entity_suffix}-{uuid4().hex[:8]}",
            created_by="svc:asset-registration",
            tenant_id=_TENANT,
            generation_task_id=task_id,
            project_id=project_id,
            asset_version_id="",
            message="",
            status=AssetRegistrationReceiptStatus.REJECTED,
            registration_fingerprint=fingerprint,
        )

    async with asset_sm() as session:  # type: ignore[operator]
        await set_tenant_context(session, _TENANT)
        repo = SqlAlchemyAssetRegistrationReceiptRepository(session)
        first = make_receipt("first", "a" * 64)
        await repo.add(first)
        with pytest.raises(DuplicateEntityError):
            await repo.add(make_receipt("second", "b" * 64))
        # SAVEPOINT 生效：冲突后 session 仍可用，能回读既有回执（handler 收敛前提）。
        reread = await repo.get_by_tenant_task(_TENANT, task_id)
        assert reread is not None
        assert reread.registration_fingerprint == "a" * 64
        await session.rollback()
