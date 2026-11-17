"""GenerationTask API 集成测试（异步，连真实 PostgreSQL，走真实鉴权）。

验证 GENERATION.REQUEST 受理入口与生成任务执行环：
- 受理成功落 PENDING + 审计事件（outbox），引用对象真实跨域解析；
- UNVERIFIED 路线拒绝（ACT-ROUTE-NEG-001 同源）；
- 幽灵路线 404 / 跨项目引用 422（ACT-GEN-NEG-001）；
- 失败任务重试以新任务承载（attempt+1）；
- ADR-R20 守卫触发器在数据库层拦截终态复活（FAILED→RUNNING）；
- worker 执行环：确定性适配器走完 PENDING→RUNNING→SUCCEEDED/FAILED。
"""

from __future__ import annotations

import hashlib
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from services.asset_service.domain.enums import AssetStatus
from services.asset_service.repository.sqlalchemy_asset_repo import (
    SqlAlchemyAssetRepository,
)
from services.generation_execution.domain.enums import (
    GenerationTaskStatus,
    ProductionRouteStatus,
)
from services.generation_execution.domain.generation_task import GenerationTask
from services.generation_execution.domain.production_route import ProductionRoute
from services.generation_execution.repository.generation_task_sqlalchemy_repo import (
    SqlAlchemyGenerationTaskRepository,
)
from services.generation_execution.repository.production_route_sqlalchemy_repo import (
    SqlAlchemyProductionRouteRepository,
)
from tests.conftest import TEST_TENANT_ID, apply_db_context, make_test_headers, set_raw_rls_context
from workers.generation.adapter import DeterministicGenerationAdapter
from workers.generation.asset_registrar import (
    AssetVersionRegistrar,
    derive_asset_version_id,
)
from workers.generation.executor import (
    GenerationExecutorContext,
    GenerationRoundStats,
    execute_round,
)

pytestmark = pytest.mark.asyncio

_PROJECT = "proj_gentask_001"


def _auth_headers(subject_id: str = "director_001") -> dict[str, str]:
    return make_test_headers(subject_id=subject_id, roles=["导演"])


def _new_route_id() -> str:
    return f"route_{uuid.uuid4().hex[:8]}"


@pytest.fixture
def route_id() -> str:
    """每测试一条唯一的 VERIFIED 路线 id（避免与库中残留数据撞主键）。"""
    return _new_route_id()


@pytest.fixture
def route_unv_id() -> str:
    return _new_route_id()


async def _seed_route(
    gen_sm: async_sessionmaker[AsyncSession],
    *,
    route_id: str,
    status: ProductionRouteStatus = ProductionRouteStatus.VERIFIED,
) -> None:
    """以 generation 域角色 + RLS 上下文经仓储端口登记路线（运维播种口径）。"""
    route = ProductionRoute(
        entity_id=route_id,
        route_type="blender_scripted",
        supported_control_signals=["camera_path"],
        required_appearance_refs=[],
        qc_dimensions=["geometry_validity"],
        capability_refs=(
            ["evidence_pkg_001"] if status is ProductionRouteStatus.VERIFIED else []
        ),
        status=status,
        created_by="ops_seed",
        tenant_id=TEST_TENANT_ID,
    )
    async with gen_sm() as session:
        await set_raw_rls_context(
            session, tenant_id=str(TEST_TENANT_ID)
        )
        await SqlAlchemyProductionRouteRepository(session).add(route)
        await session.commit()


async def _seed_shot_refs(
    shot_sm: async_sessionmaker[AsyncSession],
    *,
    project_id: str,
) -> tuple[str, str]:
    """在 shot_planning 域种子 ShotSpec + ShotVisualPlan（真实跨域解析目标）。"""
    from services.shot_planning.domain.shot_spec import ShotSpec
    from services.shot_planning.domain.shot_visual_plan import ShotVisualPlan
    from services.shot_planning.repository.shot_spec_sqlalchemy_repo import (
        SqlAlchemyShotSpecRepository,
    )
    from services.shot_planning.repository.shot_visual_plan_sqlalchemy_repo import (
        SqlAlchemyShotVisualPlanRepository,
    )

    spec = ShotSpec(
        entity_id=f"spec_{uuid.uuid4().hex[:8]}",
        shot_revision_id=uuid.uuid4().hex,
        shot_id="SHOT_001",
        project_id=project_id,
        duration_ms=4200,
        aspect_ratio="16:9",
        fps="24/1",
        coordinate_system="RIGHT_HANDED/Y/METER",
        camera_path={"type": "DOLLY", "keyframes": []},
        created_by="director_001",
        tenant_id=TEST_TENANT_ID,
    )
    vplan = ShotVisualPlan(
        entity_id=f"vplan_{uuid.uuid4().hex[:8]}",
        shot_id="SHOT_001",
        project_id=project_id,
        intent_summary="夜戏追车，高对比",
        camera_language="手持跟拍",
        shot_size="中景",
        lens_range="35mm",
        motion_intent="横移",
        lighting_intent="低照度",
        open_fields=["lens_range"],
        created_by="director_001",
        tenant_id=TEST_TENANT_ID,
    )
    async with shot_sm() as session:
        await apply_db_context(session, project_ids=(project_id,))
        await SqlAlchemyShotSpecRepository(session).add(spec)
        await SqlAlchemyShotVisualPlanRepository(session).add(vplan)
        await session.commit()
    return spec.entity_id, vplan.entity_id


async def _seed_task(
    gen_sm: async_sessionmaker[AsyncSession],
    *,
    entity_id: str,
    route_id: str,
    project_id: str = _PROJECT,
    status: GenerationTaskStatus = GenerationTaskStatus.FAILED,
    attempt: int = 1,
) -> GenerationTask:
    """直接以仓储端口落一个指定状态的任务（重试链/守卫测试基线）。"""
    task = GenerationTask(
        entity_id=entity_id,
        project_id=project_id,
        shot_id="SHOT_001",
        route_id=route_id,
        shot_spec_ref="spec_x",
        visual_plan_ref="vplan_x",
        constraint_snapshot_ref="NOT_APPLICABLE",
        budget_reservation_ref="budget_res_x",
        quota_reservation_ref="quota_res_x",
        dependency_refs=[],
        attempt=attempt,
        created_by="director_001",
        tenant_id=TEST_TENANT_ID,
        status=status,
    )
    async with gen_sm() as session:
        await apply_db_context(session, project_ids=(project_id,))
        await SqlAlchemyGenerationTaskRepository(session).add(task)
        await session.commit()
    return task


def _request_body(
    *,
    route_id: str,
    project_id: str = _PROJECT,
    spec_ref: str = "spec_x",
    vplan_ref: str = "vplan_x",
    constraint_ref: str = "NOT_APPLICABLE",
    retry_of: str | None = None,
) -> dict[str, object]:
    body: dict[str, object] = {
        "project_id": project_id,
        "shot_id": "SHOT_001",
        "route_id": route_id,
        "shot_spec_ref": spec_ref,
        "visual_plan_ref": vplan_ref,
        "constraint_snapshot_ref": constraint_ref,
        "budget_reservation_ref": "budget_res_001",
        "quota_reservation_ref": "quota_res_001",
        "dependency_refs": [],
    }
    if retry_of is not None:
        body["retry_of"] = retry_of
    return body


async def test_generation_request_admitted(
    auth_client: AsyncClient,
    gen_sm: async_sessionmaker[AsyncSession],
    shot_sm: async_sessionmaker[AsyncSession],
    route_id: str,
) -> None:
    """受理成功：任务落 PENDING，审计事件进 outbox，引用真实跨域解析。"""
    await _seed_route(gen_sm, route_id=route_id)
    spec_id, vplan_id = await _seed_shot_refs(shot_sm, project_id=_PROJECT)

    resp = await auth_client.post(
        "/actions/generation.request",
        headers=_auth_headers(),
        json=_request_body(route_id=route_id, spec_ref=spec_id, vplan_ref=vplan_id),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ACCEPTED"
    assert body["audit_event"] == "GENERATION_REQUESTED"
    assert body["task_status"] == "PENDING"
    assert body["attempt"] == 1

    async with gen_sm() as session:
        await set_raw_rls_context(session, tenant_id=str(TEST_TENANT_ID))
        row = (
            await session.execute(
                text(
                    "SELECT payload FROM outbox.outbox_events "
                    "WHERE event_type = 'GENERATION_REQUESTED' "
                    "AND payload->>'generation_task_id' = :tid"
                ),
                {"tid": body["generation_task_id"]},
            )
        ).scalar_one_or_none()
    assert row is not None
    assert row["project_id"] == _PROJECT
    assert row["attempt"] == 1


async def test_unverified_route_rejected(
    auth_client: AsyncClient,
    gen_sm: async_sessionmaker[AsyncSession],
    shot_sm: async_sessionmaker[AsyncSession],
    route_unv_id: str,
) -> None:
    await _seed_route(gen_sm, route_id=route_unv_id, status=ProductionRouteStatus.UNVERIFIED)
    spec_id, vplan_id = await _seed_shot_refs(shot_sm, project_id=_PROJECT)
    resp = await auth_client.post(
        "/actions/generation.request",
        headers=_auth_headers(),
        json=_request_body(
            route_id=route_unv_id, spec_ref=spec_id, vplan_ref=vplan_id
        ),
    )
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "VALIDATION_ERROR"


async def test_ghost_route_not_found(auth_client: AsyncClient) -> None:
    resp = await auth_client.post(
        "/actions/generation.request",
        headers=_auth_headers(),
        json=_request_body(route_id="route_missing"),
    )
    assert resp.status_code == 404
    assert resp.json()["error_code"] == "NOT_FOUND"


async def test_cross_project_spec_ref_rejected(
    auth_client: AsyncClient,
    gen_sm: async_sessionmaker[AsyncSession],
    shot_sm: async_sessionmaker[AsyncSession],
    route_id: str,
) -> None:
    """ACT-GEN-NEG-001：spec 属于别的项目 → 422（引用归属校验经跨域只读端口）。"""
    await _seed_route(gen_sm, route_id=route_id)
    other_project = "proj_other_gentask"
    spec_id, vplan_id = await _seed_shot_refs(shot_sm, project_id=other_project)
    resp = await auth_client.post(
        "/actions/generation.request",
        headers=_auth_headers(),
        json=_request_body(route_id=route_id, spec_ref=spec_id, vplan_ref=vplan_id),
    )
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "VALIDATION_ERROR"
    assert "ACT-GEN-NEG-001" in resp.text


async def test_retry_of_failed_task_increments_attempt(
    auth_client: AsyncClient,
    gen_sm: async_sessionmaker[AsyncSession],
    shot_sm: async_sessionmaker[AsyncSession],
    route_id: str,
) -> None:
    """重试以新任务承载：FAILED 任务可被引用，新任务 attempt+1。"""
    await _seed_route(gen_sm, route_id=route_id)
    spec_id, vplan_id = await _seed_shot_refs(shot_sm, project_id=_PROJECT)
    failed = await _seed_task(
        gen_sm, entity_id=f"gen_fail_{uuid.uuid4().hex[:8]}", route_id=route_id
    )

    resp = await auth_client.post(
        "/actions/generation.request",
        headers=_auth_headers(),
        json=_request_body(
            route_id=route_id,
            spec_ref=spec_id,
            vplan_ref=vplan_id,
            retry_of=failed.entity_id,
        ),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["attempt"] == 2


async def test_db_guard_blocks_terminal_resurrection(
    gen_sm: async_sessionmaker[AsyncSession],
    route_id: str,
) -> None:
    """ADR-R20：FAILED→RUNNING（终态复活）直连 SQL 在数据库层被拒。"""
    task = await _seed_task(
        gen_sm,
        entity_id=f"gen_term_{uuid.uuid4().hex[:8]}",
        route_id=route_id,
        status=GenerationTaskStatus.SUCCEEDED,
    )
    with pytest.raises(DBAPIError) as exc_info:
        async with gen_sm() as session:
            await set_raw_rls_context(session, tenant_id=str(TEST_TENANT_ID))
            await session.execute(
                text(
                    "UPDATE generation_execution.generation_tasks "
                    "SET status = 'RUNNING' WHERE entity_id = :rid"
                ),
                {"rid": task.entity_id},
            )
    assert "非法状态转移" in str(exc_info.value)


@pytest.mark.parametrize(
    ("column", "forged_value"),
    [
        # 预算引用：准入事实的代表列。
        ("budget_reservation_ref", "forged"),
        # 权利凭据（ADR-R33 新增列）：同样在受理时定型，事后改写等于伪造
        # "这次生成有权利背书"——批准链会因此被绕过。
        ("rights_manifest_ref", "forged_rights"),
    ],
)
async def test_db_guard_blocks_content_rewrite(
    gen_sm: async_sessionmaker[AsyncSession],
    route_id: str,
    column: str,
    forged_value: str,
) -> None:
    """受理时定型的准入事实不可事后改写（白名单之外一律不可变）。"""
    task = await _seed_task(
        gen_sm, entity_id=f"gen_mut_{uuid.uuid4().hex[:8]}", route_id=route_id
    )
    with pytest.raises(DBAPIError) as exc_info:
        async with gen_sm() as session:
            await set_raw_rls_context(session, tenant_id=str(TEST_TENANT_ID))
            await session.execute(
                text(
                    "UPDATE generation_execution.generation_tasks "
                    f"SET {column} = :value WHERE entity_id = :rid"
                ),
                {"rid": task.entity_id, "value": forged_value},
            )
    assert "不可变内容列" in str(exc_info.value)


def _executor_context(project_ids: frozenset[str]) -> GenerationExecutorContext:
    return GenerationExecutorContext(
        tenant_id=TEST_TENANT_ID,
        organization_id=TEST_TENANT_ID,
        project_ids=project_ids,
    )


async def test_worker_completes_task_lifecycle(
    gen_sm: async_sessionmaker[AsyncSession],
    asset_sm: async_sessionmaker[AsyncSession],
    route_id: str,
) -> None:
    """worker 执行环：PENDING→RUNNING→SUCCEEDED，产出登记为资产版本。

    result_ref 从段 3 的"生成 URI"收敛为**产出对象引用**（资产版本号）：
    主链的终态必须指向可交付对象，而不是一个只有适配器认识的字符串。
    """
    # 每测试唯一项目：worker 轮询按项目范围（RLS）过滤，不受库中残留任务干扰。
    project = f"proj_{uuid.uuid4().hex[:8]}"
    task = await _seed_task(
        gen_sm,
        entity_id=f"gen_run_{uuid.uuid4().hex[:8]}",
        route_id=route_id,
        project_id=project,
        status=GenerationTaskStatus.PENDING,
    )
    stats = await execute_round(
        gen_sm,
        _executor_context(frozenset({project})),
        DeterministicGenerationAdapter(),
        GenerationRoundStats(),
        registrar=AssetVersionRegistrar(asset_sessionmaker=asset_sm),
    )
    assert stats.claimed == 1
    assert stats.succeeded == 1
    assert stats.failed == 0

    async with gen_sm() as session:
        await apply_db_context(session, project_ids=(project,))
        done = await SqlAlchemyGenerationTaskRepository(session).get_by_id(
            task.entity_id
        )
    assert done is not None
    assert done.status is GenerationTaskStatus.SUCCEEDED
    # 产出对象引用 = 确定性资产版本号（同一任务必然同一 id，幂等键）。
    assert done.result_ref == derive_asset_version_id(task)

    async with asset_sm() as session:
        await apply_db_context(session, project_ids=(project,))
        asset = await SqlAlchemyAssetRepository(session).get_by_id(done.result_ref)
    assert asset is not None
    assert asset.status is AssetStatus.GENERATED
    assert asset.project_id == project
    # 产出与路线的关联必须可追溯：出了质量问题要能回答"哪条路线做的"。
    assert asset.adapter_id == route_id
    assert asset.sha256 == _expected_digest(task.entity_id, attempt=1)
    # 种子任务没有权利凭据 → 资产照样登记但**不可批准**（fail-closed）。
    assert asset.rights_manifest_ref is None


def _expected_digest(task_id: str, *, attempt: int) -> str:
    """确定性适配器的摘要口径（与 workers.generation.adapter 同源）。"""
    return hashlib.sha256(
        f"genresult://{task_id}/attempt-{attempt}".encode()
    ).hexdigest()


class _FailingRegistrar:
    """登记器故障桩：模拟资产域不可写（DG 断连 / 权限/ 校验失败）。"""

    async def register(self, *, task: object, output: object, principal: object) -> None:
        raise RuntimeError("asset domain unavailable")


async def test_worker_marks_failed_when_output_registration_fails(
    gen_sm: async_sessionmaker[AsyncSession],
    asset_sm: async_sessionmaker[AsyncSession],
    route_id: str,
) -> None:
    """产出登记失败 → FAILED（而不是 SUCCEEDED + 空产出）。

    「生成成功但没有资产」如果记成成功，下游 QC/交付会拿到一个指向不存在
    资产的 result_ref，主链在无告警的情况下断开——这是本用例要挡住的状态。
    """
    project = f"proj_{uuid.uuid4().hex[:8]}"
    task = await _seed_task(
        gen_sm,
        entity_id=f"gen_noreg_{uuid.uuid4().hex[:8]}",
        route_id=route_id,
        project_id=project,
        status=GenerationTaskStatus.PENDING,
    )
    stats = await execute_round(
        gen_sm,
        _executor_context(frozenset({project})),
        DeterministicGenerationAdapter(),
        GenerationRoundStats(),
        registrar=_FailingRegistrar(),
    )
    assert (stats.claimed, stats.succeeded, stats.failed) == (1, 0, 1)

    async with gen_sm() as session:
        await apply_db_context(session, project_ids=(project,))
        done = await SqlAlchemyGenerationTaskRepository(session).get_by_id(
            task.entity_id
        )
    assert done is not None
    assert done.status is GenerationTaskStatus.FAILED
    assert done.result_ref is None
    # 失败分类可区分「供应商没做出来」与「做出来但没落地」。
    assert "output_registration" in (done.audit_ref or "")

    async with asset_sm() as session:
        await apply_db_context(session, project_ids=(project,))
        assert (
            await SqlAlchemyAssetRepository(session).get_by_id(
                derive_asset_version_id(task)
            )
            is None
        )


async def test_worker_failure_path_is_terminal_and_retryable_via_new_task(
    auth_client: AsyncClient,
    gen_sm: async_sessionmaker[AsyncSession],
    asset_sm: async_sessionmaker[AsyncSession],
    shot_sm: async_sessionmaker[AsyncSession],
    route_id: str,
) -> None:
    """适配器失败 → FAILED 终态；重试经 API 以新任务承载（attempt+1）。"""
    project = f"proj_{uuid.uuid4().hex[:8]}"
    task = await _seed_task(
        gen_sm,
        entity_id=f"gen_bad_{uuid.uuid4().hex[:8]}",
        route_id=route_id,
        project_id=project,
        status=GenerationTaskStatus.PENDING,
    )
    stats = await execute_round(
        gen_sm,
        _executor_context(frozenset({project})),
        DeterministicGenerationAdapter(fail_task_ids=frozenset({task.entity_id})),
        GenerationRoundStats(),
        registrar=AssetVersionRegistrar(asset_sessionmaker=asset_sm),
    )
    assert stats.failed == 1

    async with gen_sm() as session:
        await apply_db_context(session, project_ids=(project,))
        done = await SqlAlchemyGenerationTaskRepository(session).get_by_id(
            task.entity_id
        )
    assert done is not None
    assert done.status is GenerationTaskStatus.FAILED
    assert done.result_ref is None

    # 失败任务不产出资产：登记发生在执行成功之后，失败路径不允许留下半成品。
    async with asset_sm() as session:
        await apply_db_context(session, project_ids=(project,))
        leftover = await SqlAlchemyAssetRepository(session).get_by_id(
            derive_asset_version_id(task)
        )
    assert leftover is None

    # 失败任务不复活：经 API 重试 → 新任务 attempt+1。
    await _seed_route(gen_sm, route_id=route_id)
    spec_id, vplan_id = await _seed_shot_refs(shot_sm, project_id=project)
    resp = await auth_client.post(
        "/actions/generation.request",
        headers=_auth_headers(),
        json=_request_body(
            route_id=route_id,
            project_id=project,
            spec_ref=spec_id,
            vplan_ref=vplan_id,
            retry_of=task.entity_id,
        ),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["attempt"] == 2
