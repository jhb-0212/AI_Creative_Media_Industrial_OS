"""资产登记 Saga 的结构性验收（ADR-R42 C1/C2/C4/C5，真实 PostgreSQL）。

为什么测库而不是测服务
----------------------
本 PR 没有任何业务处理器——T0~T3 的实现在后续 PR。现在要证明的是「契约声明的
不变量在数据库里成立」：跨租户伪造登记依据写不进去、产物哈希与定位符不能互相
矛盾、回执一旦落库谁都改不掉。这些若只写在服务层，直连 SQL、运维脚本与未来的
新写入口都能绕过。

判据遵循两条铁律：
1. catalog 实读——约束名、``convalidated``、触发器都从系统目录查，不认「代码里
   写了」；列不存在判「不可计算」，不能当成 0 条违规。
2. 写入类用例必须捕获期望的 SQLSTATE，**再回读确认没写进去**；RLS 可能把越权
   写静默过滤成 0 行，「没抛异常」不是 PASS。

测试自造全部前置行，使用每次运行的新 id（回执是 append-only，不能靠清理重跑）。
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

_FK_VIOLATION = "23503"
_CHECK_VIOLATION = "23514"
_RESTRICT_VIOLATION = "23001"


def _sqlstate(exc: DBAPIError) -> str | None:
    return getattr(exc.orig, "sqlstate", None)


@pytest.fixture(scope="module")
async def admin_engine() -> AsyncEngine:
    """迁移角色（超级用户）连库：RLS 不干扰，专门验证结构层防线。"""
    engine = create_async_engine(build_migration_url(), pool_pre_ping=True)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture(scope="module")
async def sample(admin_engine: AsyncEngine) -> AsyncIterator[dict[str, object]]:
    """造齐两张新表的前置行：项目 + 生成任务，均用本次运行的唯一 id。"""
    run = uuid.uuid4()
    tenant = uuid.UUID("00000000-0000-0000-0000-000000000031")
    other_tenant = uuid.UUID("00000000-0000-0000-0000-000000000032")
    project_id = f"reg-proj-{run}"
    other_project_id = f"reg-other-{run}"
    task_id = f"reg-task-{run}"
    task_entity = f"reg-task-entity-{run}"
    now = datetime.now(UTC)

    async with admin_engine.begin() as conn:
        for pid, tid, name in (
            (project_id, tenant, "登记样本项目"),
            (other_project_id, other_tenant, "异租户项目"),
        ):
            await conn.execute(
                sa.text(
                    "INSERT INTO project_planning.projects "
                    "(entity_id, entity_type, schema_version, entity_version, status, "
                    " owner_module, tenant_id, created_at, created_by, updated_at, "
                    " project_name, project_tier, organization_id) "
                    "VALUES (:pid, 'Project', 205, 1, 'DRAFT', 'project_planning', "
                    " :tid, :now, 'test-seeder', :now, :name, 'B', NULL)"
                ),
                {"pid": pid, "tid": tid, "now": now, "name": name},
            )
        await conn.execute(
            sa.text(
                "INSERT INTO production_planning.generation_tasks "
                "(entity_id, entity_type, schema_version, entity_version, status, "
                " owner_module, tenant_id, created_at, created_by, updated_at, "
                " generation_task_id, project_id, shot_id, shot_revision_id, "
                " production_route_ref, model_adapter_ref, constraint_snapshot_id, "
                " budget_ref, prompt_ref, lease_owner, attempt_count, "
                " asset_version_id, quality_profile_version) "
                "VALUES (:eid, 'GenerationTask', 205, 1, 'REQUESTED', "
                " 'production_planning', :tid, :now, 'test-seeder', :now, "
                " :task_id, :pid, '', '', '', '', '', '', '', '', 0, '', :qp)"
            ),
            {
                "eid": task_entity, "tid": tenant, "now": now,
                "task_id": task_id, "pid": project_id, "qp": UNIFIED_HIGH_QUALITY,
            },
        )
    payload: dict[str, object] = {
        "tenant": tenant,
        "other_tenant": other_tenant,
        "project_id": project_id,
        "other_project_id": other_project_id,
        "task_id": task_id,
        "task_entity": task_entity,
        "now": now,
        "run": run,
    }
    try:
        yield payload
    finally:
        # CI 在 pytest 之后做 downgrade -1：旧 CHECK 不认新等待态。测试必须
        # 把自己造的在途任务复位，不能要求流水线先人工清库才能回放迁移。
        async with admin_engine.begin() as conn:
            await conn.execute(
                sa.text(
                    "UPDATE production_planning.generation_tasks "
                    "SET status = 'SUCCEEDED' "
                    "WHERE status = 'AWAITING_ASSET_REGISTRATION' "
                    "AND generation_task_id LIKE :pattern"
                ),
                {"pattern": f"%{run}%"},
            )


def _manifest_row(sample: dict[str, object], *, task_id: str | None = None,
                  project_id: str | None = None, digest: str = "a" * 64,
                  entity_id: str | None = None) -> dict[str, object]:
    return {
        # entity_id 列是 String(64)：uuid4 的 36 字符加上前缀会超长，
        # 超长报的是 22001，会伪装成「外键/CHECK 没生效」的假红灯。
        "eid": entity_id or f"m-{uuid.uuid4().hex[:24]}",
        "tid": sample["tenant"],
        "now": sample["now"],
        "task_id": task_id or sample["task_id"],
        "pid": project_id or sample["project_id"],
        "locator": build_locator(digest, "png"),
        "sha": digest,
        "attempt": f"attempt-{uuid.uuid4()}",
    }


_INSERT_MANIFEST = sa.text(
    "INSERT INTO production_planning.artifact_manifests "
    "(entity_id, entity_type, schema_version, entity_version, status, owner_module, "
    " tenant_id, created_at, created_by, updated_at, generation_task_id, project_id, "
    " locator, sha256, attempt_id) "
    "VALUES (:eid, 'ArtifactManifest', 205, 1, 'PREPARED', 'production_planning', "
    " :tid, :now, 'test-seeder', :now, :task_id, :pid, :locator, :sha, :attempt)"
)


async def test_catalog_facts_are_readable_and_true(admin_engine: AsyncEngine) -> None:
    """约束/触发器从 catalog 实读；列不存在时判不可计算，不伪装成 0 违规。"""
    async with admin_engine.connect() as conn:
        cols = await conn.execute(
            sa.text(
                "SELECT table_schema, table_name, column_name "
                "FROM information_schema.columns "
                "WHERE (table_schema, table_name) IN "
                "(('production_planning','artifact_manifests'), "
                " ('asset_service','asset_registration_receipts'))"
            )
        )
        actual = {(r[0], r[1], r[2]) for r in cols}
        for schema, table in (
            ("production_planning", "artifact_manifests"),
            ("asset_service", "asset_registration_receipts"),
        ):
            for col in ("project_id", "tenant_id", "generation_task_id", "status"):
                assert (schema, table, col) in actual, f"{schema}.{table}.{col} 不存在"

        fks = await conn.execute(
            sa.text(
                "SELECT conname, convalidated FROM pg_constraint "
                "WHERE conrelid IN "
                "('production_planning.artifact_manifests'::regclass, "
                " 'asset_service.asset_registration_receipts'::regclass) "
                "AND contype = 'f' ORDER BY conname"
            )
        )
        # CursorResult 自带 keys()，dict(cursor) 会把它当映射并尝试字符串下标；
        # 先取成行列表，再按 (约束名, convalidated) 建字典。
        fk_map = dict(fks.all())
        for name in (
            "fk_artifact_manifests_project_tenant",
            "fk_artifact_manifests_generation_task",
            "fk_asset_registration_receipts_project_tenant",
            "fk_asset_registration_receipts_generation_task",
            "fk_asset_registration_receipts_asset_version",
        ):
            assert name in fk_map, f"缺复合外键 {name}"
            # CREATE TABLE 内建的约束天然 convalidated；台账须如实反映这一点。
            assert fk_map[name] is True, f"{name} 未验证"

        asset_key = await conn.scalar(
            sa.text(
                "SELECT convalidated FROM pg_constraint "
                "WHERE conrelid = 'asset_service.asset_versions'::regclass "
                "AND conname = 'uq_asset_versions_project_entity' AND contype = 'u'"
            )
        )
        assert asset_key is True, "资产侧缺 (project_id, entity_id) 唯一目标键"

        task_status = await conn.scalar(
            sa.text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conrelid = 'production_planning.generation_tasks'::regclass "
                "AND conname = 'ck_generation_tasks_status'"
            )
        )
        assert task_status and "AWAITING_ASSET_REGISTRATION" in task_status

        triggers = {
            r[0]
            for r in await conn.execute(
                sa.text(
                    "SELECT tgname FROM pg_trigger "
                    "WHERE tgrelid = 'asset_service.asset_registration_receipts'::regclass "
                    "AND NOT tgisinternal"
                )
            )
        }
        assert "trg_asset_registration_receipts_immutable" in triggers
        assert "trg_asset_registration_receipts_no_truncate" in triggers


async def test_manifest_rejects_cross_tenant_project(
    admin_engine: AsyncEngine, sample: dict[str, object]
) -> None:
    """跨租户挂靠必须 23503，且回读确认没有写入。"""
    row = _manifest_row(sample, project_id=str(sample["other_project_id"]))
    with pytest.raises(DBAPIError) as caught:
        async with admin_engine.begin() as conn:
            await conn.execute(_INSERT_MANIFEST, row)
    assert _sqlstate(caught.value) == _FK_VIOLATION

    async with admin_engine.connect() as conn:
        count = await conn.scalar(
            sa.text(
                "SELECT count(*) FROM production_planning.artifact_manifests "
                "WHERE entity_id = :eid"
            ),
            {"eid": row["eid"]},
        )
    assert count == 0


async def test_manifest_rejects_unknown_task(
    admin_engine: AsyncEngine, sample: dict[str, object]
) -> None:
    """登记依据必须指向真实生成任务（复合键含任务/项目/租户）。"""
    row = _manifest_row(sample, task_id=f"no-such-task-{uuid.uuid4()}")
    with pytest.raises(DBAPIError) as caught:
        async with admin_engine.begin() as conn:
            await conn.execute(_INSERT_MANIFEST, row)
    assert _sqlstate(caught.value) == _FK_VIOLATION


async def test_manifest_rejects_locator_digest_mismatch(
    admin_engine: AsyncEngine, sample: dict[str, object]
) -> None:
    """定位符内嵌哈希与 sha256 列矛盾时必须 23514（内容身份只能是单列事实）。"""
    row = _manifest_row(sample, digest="b" * 64)
    row["sha"] = "c" * 64
    with pytest.raises(DBAPIError) as caught:
        async with admin_engine.begin() as conn:
            await conn.execute(_INSERT_MANIFEST, row)
    assert _sqlstate(caught.value) == _CHECK_VIOLATION


async def test_receipt_is_append_only_even_for_superuser(
    admin_engine: AsyncEngine, sample: dict[str, object]
) -> None:
    """回执落库后不可原地改写；RLS 管不到的超管连接也必须被触发器拦住。"""
    eid = f"reg-receipt-{sample['run']}"
    params = {
        "eid": eid,
        "tid": sample["tenant"],
        "now": sample["now"],
        "task_id": sample["task_id"],
        "pid": sample["project_id"],
    }
    async with admin_engine.begin() as conn:
        result = await conn.execute(
            sa.text(
                "INSERT INTO asset_service.asset_registration_receipts "
                "(entity_id, entity_type, schema_version, entity_version, status, "
                " owner_module, tenant_id, created_at, created_by, updated_at, "
                " generation_task_id, project_id, asset_version_id, message) "
                "VALUES (:eid, 'AssetRegistrationReceipt', 205, 1, 'REJECTED', "
                " 'asset_service', :tid, :now, 'test-seeder', :now, "
                " :task_id, :pid, NULL, 'sha256 冲突')"
            ),
            params,
        )
        assert result.rowcount == 1

    for sql in (
        "UPDATE asset_service.asset_registration_receipts SET message = '改写' "
        "WHERE entity_id = :eid",
        "DELETE FROM asset_service.asset_registration_receipts WHERE entity_id = :eid",
    ):
        with pytest.raises(DBAPIError) as caught:
            async with admin_engine.begin() as conn:
                await conn.execute(sa.text(sql), {"eid": eid})
        assert _sqlstate(caught.value) == _RESTRICT_VIOLATION

    # 回读确认：UPDATE/DELETE 都没生效，原事实仍在。
    async with admin_engine.connect() as conn:
        row = (
            await conn.execute(
                sa.text(
                    "SELECT status, message FROM asset_service.asset_registration_receipts "
                    "WHERE entity_id = :eid"
                ),
                {"eid": eid},
            )
        ).one()
    assert row.status == "REJECTED" and row.message == "sha256 冲突"


async def test_completed_receipt_requires_asset_id(
    admin_engine: AsyncEngine, sample: dict[str, object]
) -> None:
    """COMPLETED 却无 asset_version_id 是自相矛盾的回执，必须在库层拒绝。"""
    params = {
        "eid": f"reg-receipt-bad-{sample['run']}",
        "tid": sample["tenant"],
        "now": sample["now"],
        "task_id": f"reg-task-bad-{sample['run']}",
        "pid": sample["project_id"],
    }
    async with admin_engine.begin() as conn:
        await conn.execute(
            sa.text(
                "INSERT INTO production_planning.generation_tasks "
                "(entity_id, entity_type, schema_version, entity_version, status, "
                " owner_module, tenant_id, created_at, created_by, updated_at, "
                " generation_task_id, project_id, shot_id, shot_revision_id, "
                " production_route_ref, model_adapter_ref, constraint_snapshot_id, "
                " budget_ref, prompt_ref, lease_owner, attempt_count, "
                " asset_version_id, quality_profile_version) "
                "VALUES (:eid, 'GenerationTask', 205, 1, 'REQUESTED', "
                " 'production_planning', :tid, :now, 'test-seeder', :now, "
                " :task_id, :pid, '', '', '', '', '', '', '', '', 0, '', '')"
            ),
            {
                "eid": f"reg-task-entity-bad-{sample['run']}",
                "tid": sample["tenant"], "now": sample["now"],
                "task_id": params["task_id"], "pid": sample["project_id"],
            },
        )
    with pytest.raises(DBAPIError) as caught:
        async with admin_engine.begin() as conn:
            await conn.execute(
                sa.text(
                    "INSERT INTO asset_service.asset_registration_receipts "
                    "(entity_id, entity_type, schema_version, entity_version, status, "
                    " owner_module, tenant_id, created_at, created_by, updated_at, "
                    " generation_task_id, project_id, asset_version_id) "
                    "VALUES (:eid, 'AssetRegistrationReceipt', 205, 1, 'COMPLETED', "
                    " 'asset_service', :tid, :now, 'test-seeder', :now, "
                    " :task_id, :pid, NULL)"
                ),
                params,
            )
    assert _sqlstate(caught.value) == _CHECK_VIOLATION


async def test_receipt_rejects_asset_from_another_project_same_tenant(
    admin_engine: AsyncEngine, sample: dict[str, object]
) -> None:
    """单列资产 ID 挡不住同租户跨项目挂靠；复合外键必须 23503 且回读为 0。"""
    run = sample["run"]
    other_project = f"reg-xasset-proj-{run}"
    task_id = f"reg-task-xasset-{run}"
    task_entity = f"reg-task-entity-xasset-{run}"
    asset_id = f"reg-asset-xasset-{run}"
    digest = "d" * 64
    params = {
        "tid": sample["tenant"],
        "now": sample["now"],
        "opid": other_project,
        "task_id": task_id,
        "task_entity": task_entity,
        "pid": sample["project_id"],
        "asset_id": asset_id,
        "digest": digest,
        "locator": build_locator(digest, "png"),
        "qp": UNIFIED_HIGH_QUALITY,
    }
    async with admin_engine.begin() as conn:
        await conn.execute(
            sa.text(
                "INSERT INTO project_planning.projects "
                "(entity_id, entity_type, schema_version, entity_version, status, "
                " owner_module, tenant_id, created_at, created_by, updated_at, "
                " project_name, project_tier, organization_id) "
                "VALUES (:opid, 'Project', 205, 1, 'DRAFT', 'project_planning', "
                " :tid, :now, 'test-seeder', :now, '同租户异项目', 'B', NULL)"
            ),
            params,
        )
        await conn.execute(
            sa.text(
                "INSERT INTO asset_service.asset_versions "
                "(entity_id, entity_type, schema_version, entity_version, status, "
                " owner_module, tenant_id, created_at, created_by, updated_at, "
                " project_id, asset_type, sha256, storage_ref, source_asset_version_ids) "
                "VALUES (:asset_id, 'AssetVersion', 205, 1, 'GENERATED', "
                " 'asset_service', :tid, :now, 'test-seeder', :now, "
                " :opid, 'IMAGE', :digest, :locator, '[]'::jsonb)"
            ),
            params,
        )
        await conn.execute(
            sa.text(
                "INSERT INTO production_planning.generation_tasks "
                "(entity_id, entity_type, schema_version, entity_version, status, "
                " owner_module, tenant_id, created_at, created_by, updated_at, "
                " generation_task_id, project_id, shot_id, shot_revision_id, "
                " production_route_ref, model_adapter_ref, constraint_snapshot_id, "
                " budget_ref, prompt_ref, lease_owner, attempt_count, "
                " asset_version_id, quality_profile_version) "
                "VALUES (:task_entity, 'GenerationTask', 205, 1, 'REQUESTED', "
                " 'production_planning', :tid, :now, 'test-seeder', :now, "
                " :task_id, :pid, '', '', '', '', '', '', '', '', 0, '', :qp)"
            ),
            params,
        )

    receipt_eid = f"reg-receipt-xasset-{run}"
    receipt_params = dict(params, eid=receipt_eid)
    with pytest.raises(DBAPIError) as caught:
        async with admin_engine.begin() as conn:
            await conn.execute(
                sa.text(
                    "INSERT INTO asset_service.asset_registration_receipts "
                    "(entity_id, entity_type, schema_version, entity_version, status, "
                    " owner_module, tenant_id, created_at, created_by, updated_at, "
                    " generation_task_id, project_id, asset_version_id) "
                    "VALUES (:eid, 'AssetRegistrationReceipt', 205, 1, 'COMPLETED', "
                    " 'asset_service', :tid, :now, 'test-seeder', :now, "
                    " :task_id, :pid, :asset_id)"
                ),
                receipt_params,
            )
    assert _sqlstate(caught.value) == _FK_VIOLATION

    async with admin_engine.connect() as conn:
        count = await conn.scalar(
            sa.text(
                "SELECT count(*) FROM asset_service.asset_registration_receipts "
                "WHERE entity_id = :eid"
            ),
            {"eid": receipt_eid},
        )
    assert count == 0


async def test_awaiting_state_and_frozen_quality_profile_are_writable(
    admin_engine: AsyncEngine, sample: dict[str, object]
) -> None:
    """契约新增的等待态与质量策略字段必须能真实落库并回读。"""
    async with admin_engine.begin() as conn:
        result = await conn.execute(
            sa.text(
                "UPDATE production_planning.generation_tasks "
                "SET status = 'AWAITING_ASSET_REGISTRATION' "
                "WHERE entity_id = :eid"
            ),
            {"eid": sample["task_entity"]},
        )
        assert result.rowcount == 1
    async with admin_engine.connect() as conn:
        status = await conn.scalar(
            sa.text(
                "SELECT status FROM production_planning.generation_tasks "
                "WHERE entity_id = :eid"
            ),
            {"eid": sample["task_entity"]},
        )
    assert status == "AWAITING_ASSET_REGISTRATION"
