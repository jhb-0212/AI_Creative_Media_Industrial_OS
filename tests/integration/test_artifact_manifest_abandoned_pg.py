"""``ArtifactManifest.ABANDONED`` 在真实库上可落库，且未知状态仍被拒（PR-G-3）。

为什么需要这条用例
------------------
领域层与静态契约都早已接受 ``ABANDONED``（状态机有边、``object_schemas`` 有枚举、
单测覆盖冻结终态），但数据库约束停在两值——两处各自都对，合起来却让「放弃」在
真实库上写不进去。这类缺口只有「在真实库上写一次」能发现。

两个方向同时断言
----------------
1. 三值都允许（``ABANDONED`` 能写进去）；
2. 第四值仍被拒（``23514``）。

只验 1 是不够的：修约束时把 ``drop_constraint`` 写成了「删掉不再加回」，
方向 1 照样通过，而约束已经从「枚举校验」退化成「什么都能写」——那比原来的
缺一个值更糟。

清理
----
``e7f8a9b0c1d2`` 的 downgrade 在存在 ABANDONED 行时会显式失败（不静默改写事实），
而 CI 在 pytest 之后就跑 ``downgrade -1``。因此本用例把自己造的行全部删除，
不给流水线留「必须先人工清库」的隐性前提。

顺带一个实证：这里确实能 ``DELETE`` 掉 ``artifact_manifests`` 的行。P2 追认的
文本要求「不得删除原始意图行」，但该表既非 append-only、也没有拒绝删除的触发器，
这条约束目前只由状态机与调用纪律承担——与 ``V205-R42-P2-RATIFY`` 的残余风险一致。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from packages.shared.database import build_migration_url
from packages.shared.quality_profile import UNIFIED_HIGH_QUALITY
from packages.shared.storage_locator import build_locator

_CHECK_VIOLATION = "23514"

_INSERT_PROJECT = sa.text(
    "INSERT INTO project_planning.projects "
    "(entity_id, entity_type, schema_version, entity_version, status, owner_module, "
    " tenant_id, created_at, created_by, updated_at, "
    " project_name, project_tier, organization_id) "
    "VALUES (:pid, 'Project', 205, 1, 'DRAFT', 'project_planning', "
    " :tid, :now, 'test-seeder', :now, :name, 'B', NULL)"
)

_INSERT_TASK = sa.text(
    "INSERT INTO production_planning.generation_tasks "
    "(entity_id, entity_type, schema_version, entity_version, status, owner_module, "
    " tenant_id, created_at, created_by, updated_at, generation_task_id, project_id, "
    " shot_id, shot_revision_id, production_route_ref, model_adapter_ref, "
    " constraint_snapshot_id, budget_ref, prompt_ref, lease_owner, attempt_count, "
    " asset_version_id, quality_profile_version) "
    "VALUES (:eid, 'GenerationTask', 205, 1, 'REQUESTED', 'production_planning', "
    " :tid, :now, 'test-seeder', :now, :task_id, :pid, "
    " '', '', '', '', '', '', '', '', 0, '', :qp)"
)

_INSERT_MANIFEST = sa.text(
    "INSERT INTO production_planning.artifact_manifests "
    "(entity_id, entity_type, schema_version, entity_version, status, owner_module, "
    " tenant_id, created_at, created_by, updated_at, "
    " generation_task_id, project_id, locator, sha256, media_type, attempt_id) "
    "VALUES (:eid, 'ArtifactManifest', 205, 1, 'PREPARED', 'production_planning', "
    " :tid, :now, 'test-seeder', :now, "
    " :task_id, :pid, :locator, :sha, 'image/png', :attempt)"
)


@pytest.fixture(scope="module")
async def admin_engine() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(build_migration_url(), pool_pre_ping=True)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def seeded(admin_engine: AsyncEngine) -> AsyncIterator[dict[str, object]]:
    """造一条合法 PREPARED 意图（项目 → 生成任务 → 意图，逐层满足外键）。"""
    run = uuid.uuid4()
    tenant = uuid.UUID("00000000-0000-0000-0000-000000000041")
    project_id = f"abn-proj-{run}"
    task_id = f"abn-task-{run}"
    manifest_id = f"abn-mani-{run}"
    digest = uuid.uuid4().hex + uuid.uuid4().hex  # 64 位 hex，满足 sha256 形态
    now = datetime.now(UTC)
    payload: dict[str, object] = {
        "run": run,
        "manifest_id": manifest_id,
    }

    async with admin_engine.begin() as conn:
        await conn.execute(
            _INSERT_PROJECT,
            {"pid": project_id, "tid": tenant, "now": now, "name": "放弃态样本项目"},
        )
        await conn.execute(
            _INSERT_TASK,
            {
                "eid": f"abn-task-entity-{run}",
                "tid": tenant,
                "now": now,
                "task_id": task_id,
                "pid": project_id,
                "qp": UNIFIED_HIGH_QUALITY,
            },
        )
        await conn.execute(
            _INSERT_MANIFEST,
            {
                "eid": manifest_id,
                "tid": tenant,
                "now": now,
                "task_id": task_id,
                "pid": project_id,
                "locator": build_locator(digest, "png"),
                "sha": digest,
                "attempt": f"attempt-{run}",
            },
        )

    try:
        yield payload
    finally:
        # 逆外键顺序清理：意图 → 任务 → 项目。留下 ABANDONED 行会让 CI 的
        # downgrade -1 卡在 e7f8a9b0c1d2 的显式失败上。
        async with admin_engine.begin() as conn:
            await conn.execute(
                sa.text(
                    "DELETE FROM production_planning.artifact_manifests WHERE entity_id = :eid"
                ),
                {"eid": manifest_id},
            )
            await conn.execute(
                sa.text(
                    "DELETE FROM production_planning.generation_tasks "
                    "WHERE generation_task_id = :tid"
                ),
                {"tid": task_id},
            )
            await conn.execute(
                sa.text("DELETE FROM project_planning.projects WHERE entity_id = :pid"),
                {"pid": project_id},
            )


async def test_abandoned_is_writable_and_unknown_state_still_rejected(
    admin_engine: AsyncEngine, seeded: dict[str, object]
) -> None:
    manifest_id = str(seeded["manifest_id"])

    async with admin_engine.begin() as conn:
        await conn.execute(
            sa.text(
                "UPDATE production_planning.artifact_manifests "
                "SET status = 'ABANDONED', entity_version = entity_version + 1, "
                "    audit_ref = :ref "
                "WHERE entity_id = :eid"
            ),
            {"ref": f"t1-abandon:object-absent:{manifest_id}", "eid": manifest_id},
        )

    async with admin_engine.connect() as conn:
        status = await conn.scalar(
            sa.text(
                "SELECT status FROM production_planning.artifact_manifests WHERE entity_id = :eid"
            ),
            {"eid": manifest_id},
        )
    assert status == "ABANDONED", f"放弃态未落库，实际 {status!r}"

    # 约束没有被放宽成「什么都行」：不在契约枚举里的值仍必须被拒。
    with pytest.raises(DBAPIError) as exc_info:
        async with admin_engine.begin() as conn:
            await conn.execute(
                sa.text(
                    "UPDATE production_planning.artifact_manifests "
                    "SET status = 'PENDING' WHERE entity_id = :eid"
                ),
                {"eid": manifest_id},
            )
    assert getattr(exc_info.value.orig, "sqlstate", None) == _CHECK_VIOLATION, (
        f"未知状态未被 CHECK 拒绝：{exc_info.value}"
    )

    # 拒绝之后仍是 ABANDONED：失败的那次 UPDATE 没有留下半截写入。
    async with admin_engine.connect() as conn:
        status = await conn.scalar(
            sa.text(
                "SELECT status FROM production_planning.artifact_manifests WHERE entity_id = :eid"
            ),
            {"eid": manifest_id},
        )
    assert status == "ABANDONED"
