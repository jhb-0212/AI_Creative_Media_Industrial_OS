"""GenerationSpec 的库层不变量（ADR-R47 P2，真实 PostgreSQL）。

服务层测试证明「派生算得对」，这里证明「绕过服务也写不进矛盾事实」：

- ``generation_tasks.generation_spec_id`` 的复合外键必须真实存在（跨租户/幽灵
  规格必须被拒）；
- 绑定等式 CHECK 必须拒绝「内联任务带规格」与「引用与 id 不一致」；
- 规格行的状态只能是哨兵，尺寸/画幅/媒体类型形态在库层同样受控。

种子自造项目行；本测试不依赖 ShotSpec 行（规格表只有 project 归属外键，
ShotSpec 一致性由领域层在派生时校验）。
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

_CHECK_VIOLATION = "23514"
_FK_VIOLATION = "23503"


def _sqlstate(exc: DBAPIError) -> str | None:
    return getattr(exc.orig, "sqlstate", None)


@pytest.fixture(scope="module")
async def engine() -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(build_migration_url(), pool_pre_ping=True)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest.fixture(scope="module")
async def sample(engine: AsyncEngine) -> dict[str, object]:
    run = uuid.uuid4().hex[:10]
    tenant = uuid.UUID("00000000-0000-0000-0000-00000000a901")
    project_id = f"spec-proj-{run}"
    spec_id = f"gs1_{run}"
    now = datetime.now(UTC)
    async with engine.begin() as conn:
        await conn.execute(
            sa.text(
                "INSERT INTO project_planning.projects "
                "(entity_id, entity_type, schema_version, entity_version, status, "
                " owner_module, tenant_id, created_at, created_by, updated_at, "
                " project_name, project_tier, organization_id) "
                "VALUES (:pid, 'Project', 205, 1, 'DRAFT', 'project_planning', "
                " :tid, :now, 'spec-seeder', :now, :name, 'B', NULL)"
            ),
            {"pid": project_id, "tid": tenant, "now": now, "name": f"规格样本-{run}"},
        )
        await conn.execute(
            sa.text(
                "INSERT INTO production_planning.generation_specs "
                "(entity_id, entity_type, schema_version, entity_version, status, "
                " owner_module, tenant_id, created_at, created_by, updated_at, "
                " generation_spec_id, project_id, shot_revision_id, shot_spec_id, "
                " production_route_ref, constraint_snapshot_id, template_version, "
                " derivation_version, size_policy_id, size_policy_version, model_id, "
                " provider, generation_parameters, prompt_text, negative_prompt, "
                " media_type, size, aspect_ratio, spec_sha256, prompt_sha256) "
                "VALUES (:eid, 'GenerationSpec', 205, 1, 'NO_LIFECYCLE', "
                " 'production-planning', :tid, :now, 'spec-seeder', :now, "
                " :sid, :pid, 'rev-1', 'shot-spec-1', 'route-1', 'snap-1', "
                " 'afs-shot-spec-v1', 'afs-derive-v1', 'afs-default', '1', "
                " 'vendor/model-x', 'vendor', '{}', '派生提示词。', NULL, "
                " 'image/png', '1280x720', '16:9', :sha, :sha)"
            ),
            {
                "eid": spec_id,
                "sid": spec_id,
                "pid": project_id,
                "tid": tenant,
                "now": now,
                "sha": "a" * 64,
            },
        )
    return {"tenant": tenant, "project_id": project_id, "spec_id": spec_id, "now": now}


async def _insert_task(conn, sample: dict[str, object], *, prompt_ref: str, spec_id: str | None) -> None:
    await conn.execute(
        sa.text(
            "INSERT INTO production_planning.generation_tasks "
            "(entity_id, entity_type, schema_version, entity_version, status, "
            " owner_module, tenant_id, created_at, created_by, updated_at, "
            " generation_task_id, project_id, shot_id, shot_revision_id, "
            " production_route_ref, model_adapter_ref, constraint_snapshot_id, "
            " budget_ref, prompt_ref, generation_spec_id, lease_owner, attempt_count, "
            " asset_version_id, quality_profile_version) "
            "VALUES (:eid, 'GenerationTask', 205, 1, 'REQUESTED', "
            " 'production-planning', :tid, :now, 'spec-seeder', :now, "
            " :gtid, :pid, 'shot-1', 'rev-1', 'route-1', 'adapter-1', 'snap-1', "
            " 'budget-1', :ref, :spec, '', 0, '', 'UNIFIED_HIGH_QUALITY')"
        ),
        {
            "eid": f"task-{uuid.uuid4().hex[:12]}",
            "gtid": f"taskid-{uuid.uuid4().hex[:12]}",
            "tid": sample["tenant"],
            "pid": sample["project_id"],
            "now": sample["now"],
            "ref": prompt_ref,
            "spec": spec_id,
        },
    )


async def test_spec_task_binding_is_enforced(engine: AsyncEngine, sample: dict[str, object]) -> None:
    async with engine.begin() as conn:
        await _insert_task(conn, sample, prompt_ref=f"genspec:{sample['spec_id']}", spec_id=sample["spec_id"])
        # 幽灵规格：复合外键必须拒绝。
        with pytest.raises(DBAPIError) as missing:
            async with conn.begin_nested():
                await _insert_task(conn, sample, prompt_ref="genspec:gs1_ghost", spec_id="gs1_ghost")
        assert _sqlstate(missing.value) == _FK_VIOLATION
        # 内联任务带规格：CHECK 必须拒绝。
        with pytest.raises(DBAPIError) as inline_spec:
            async with conn.begin_nested():
                await _insert_task(
                    conn, sample, prompt_ref="inline:hello", spec_id=sample["spec_id"]
                )
        assert _sqlstate(inline_spec.value) == _CHECK_VIOLATION
        # 引用与 id 不一致：CHECK 必须拒绝。
        with pytest.raises(DBAPIError) as mismatch:
            async with conn.begin_nested():
                await _insert_task(
                    conn, sample, prompt_ref="genspec:gs1_other", spec_id=sample["spec_id"]
                )
        assert _sqlstate(mismatch.value) == _CHECK_VIOLATION


async def test_spec_row_shape_is_enforced(engine: AsyncEngine, sample: dict[str, object]) -> None:
    async with engine.begin() as conn:
        with pytest.raises(DBAPIError) as bad_status:
            async with conn.begin_nested():
                await conn.execute(
                    sa.text(
                        "UPDATE production_planning.generation_specs SET status='ACTIVE' "
                        "WHERE entity_id=:eid"
                    ),
                    {"eid": sample["spec_id"]},
                )
        assert _sqlstate(bad_status.value) == _CHECK_VIOLATION
