"""生成主链测试种子（跨测试文件共享的单一播种口径）。

为什么要有这个模块：生成受理的准入校验依赖**真实跨域对象**（VERIFIED 路线、
ShotSpec、ShotVisualPlan），集成测试与韧性测试都需要准备它们。若各写一份，
两份种子会在某次契约变更后悄悄分叉——一边还在用旧字段，测试却仍然"绿"。

播种一律经**所有者域仓储端口 + 该域角色会话 + RLS 上下文**，与生产写入路径
同口径；不绕过领域校验直接 INSERT，否则种子本身就是一处在测不到的旁路。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from services.generation_execution.domain.enums import (
    GenerationTaskStatus,
    ProductionRouteStatus,
)
from services.generation_execution.domain.generation_task import GenerationTask
from services.generation_execution.domain.production_route import ProductionRoute
from services.generation_execution.domain.shot_route_selection import (
    ShotRouteSelection,
)
from services.generation_execution.repository.generation_task_sqlalchemy_repo import (
    SqlAlchemyGenerationTaskRepository,
)
from services.generation_execution.repository.production_route_sqlalchemy_repo import (
    SqlAlchemyProductionRouteRepository,
)
from services.generation_execution.repository.shot_route_selection_sqlalchemy_repo import (
    SqlAlchemyShotRouteSelectionRepository,
)
from tests.conftest import TEST_TENANT_ID, apply_db_context, set_raw_rls_context

SessionMaker = async_sessionmaker[AsyncSession]

#: 测试默认项目 id（历史测试数据的项目名；需要隔离的用例显式传自己的）。
DEFAULT_TEST_PROJECT = "proj_gentask_001"


def new_route_id() -> str:
    """每测试唯一路线 id（避免与库中残留数据撞主键）。"""
    return f"route_{uuid.uuid4().hex[:8]}"


async def seed_route(
    gen_sm: SessionMaker,
    *,
    route_id: str,
    status: ProductionRouteStatus = ProductionRouteStatus.VERIFIED,
) -> None:
    """以 generation 域角色 + RLS 上下文登记路线（运维播种口径）。"""
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
        await set_raw_rls_context(session, tenant_id=str(TEST_TENANT_ID))
        await SqlAlchemyProductionRouteRepository(session).add(route)
        await session.commit()


async def seed_shot_refs(
    shot_sm: SessionMaker,
    *,
    project_id: str,
    shot_id: str = "SHOT_001",
) -> tuple[str, str]:
    """在 shot_planning 域种子 ShotSpec + ShotVisualPlan（真实跨域解析目标）。

    ``shot_id`` 可指定：跨镜头引用用例需要"绑定在别的镜头上"的规格，
    而请求镜头保持不变（P1-1）。
    """
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
        shot_id=shot_id,
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
        shot_id=shot_id,
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


async def seed_task(
    gen_sm: SessionMaker,
    *,
    entity_id: str,
    route_id: str,
    project_id: str = DEFAULT_TEST_PROJECT,
    status: GenerationTaskStatus = GenerationTaskStatus.FAILED,
    attempt: int = 1,
) -> GenerationTask:
    """直接以仓储端口落一个指定状态的任务（重试链/守卫/租约测试基线）。"""
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


async def seed_selection(
    gen_sm: SessionMaker,
    *,
    route_id: str,
    project_id: str = DEFAULT_TEST_PROJECT,
    shot_id: str = "SHOT_001",
) -> ShotRouteSelection:
    """播种镜头生效路线（受理前置：GENERATION.REQUEST 要求路线是生效路线）。

    经仓储端口 + RLS 写入，与 PRODUCTION.ROUTE.SELECT 的生产路径同口径
    （唯一约束、不可变守卫都参与）。用例若要验证"选路"本身的行为，请走 HTTP
    入口，不要用本函数——种子的职责只是把测试推进到"已选路"这个前提。
    """
    selection = ShotRouteSelection(
        entity_id=f"srs_{uuid.uuid4().hex[:12]}",
        project_id=project_id,
        shot_id=shot_id,
        route_id=route_id,
        created_by="producer_001",
        tenant_id=TEST_TENANT_ID,
    )
    async with gen_sm() as session:
        await apply_db_context(session, project_ids=(project_id,))
        await SqlAlchemyShotRouteSelectionRepository(session).add(selection)
        await session.commit()
    return selection


def request_body(
    *,
    route_id: str,
    project_id: str = DEFAULT_TEST_PROJECT,
    spec_ref: str = "spec_x",
    vplan_ref: str = "vplan_x",
    constraint_ref: str = "NOT_APPLICABLE",
    retry_of: str | None = None,
    shot_id: str = "SHOT_001",
) -> dict[str, Any]:
    """GENERATION.REQUEST 请求体（受理入口的唯一测试构造口径）。"""
    body: dict[str, Any] = {
        "project_id": project_id,
        "shot_id": shot_id,
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
