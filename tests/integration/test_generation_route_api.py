"""Generation Route API 集成测试（异步，连真实 PostgreSQL，走真实鉴权）。

验证 PRODUCTION.ROUTE.SELECT 主链入口与 generation_execution 域边界：
- VERIFIED 路线选中成功并落审计事件（outbox）；
- UNVERIFIED 路线拒绝（ACT-ROUTE-NEG-001，422）；
- 幽灵路线 404；
- 状态机守卫触发器在数据库层拦截非法转移（ADR-R20）；
- 跨域角色隔离：generation 域角色写不了 shot_planning 表，反之亦然。
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from services.generation_execution.domain.enums import ProductionRouteStatus
from services.generation_execution.domain.production_route import ProductionRoute
from services.generation_execution.repository.production_route_sqlalchemy_repo import (
    SqlAlchemyProductionRouteRepository,
)
from tests.conftest import make_test_headers, set_raw_rls_context

pytestmark = pytest.mark.asyncio


def _auth_headers(subject_id: str = "producer_001") -> dict[str, str]:
    return make_test_headers(subject_id=subject_id, roles=["制片"])


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
        capability_refs=["evidence_pkg_001"] if status is ProductionRouteStatus.VERIFIED else [],
        status=status,
        created_by="ops_seed",
        tenant_id=uuid.UUID("00000000-0000-0000-0000-000000000000"),
    )
    async with gen_sm() as session:
        await set_raw_rls_context(
            session, tenant_id="00000000-0000-0000-0000-000000000000"
        )
        await SqlAlchemyProductionRouteRepository(session).add(route)
        await session.commit()


async def test_select_verified_route_succeeds(
    auth_client: AsyncClient,
    gen_sm: async_sessionmaker[AsyncSession],
) -> None:
    route_id = f"route_{uuid.uuid4().hex[:8]}"
    await _seed_route(gen_sm, route_id=route_id)

    resp = await auth_client.post(
        "/actions/production.route.select",
        headers=_auth_headers(),
        json={
            "route_id": route_id,
            "project_id": "proj_genroute_001",
            "shot_id": "SHOT_001",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ACCEPTED"
    assert body["audit_event"] == "PRODUCTION_ROUTE_SELECTED"
    assert body["route_id"] == route_id

    # 审计事件确实落在 outbox（事务性 outbox，与业务提交同事务）。
    async with gen_sm() as session:
        await set_raw_rls_context(
            session, tenant_id="00000000-0000-0000-0000-000000000000"
        )
        row = (
            await session.execute(
                text(
                    "SELECT payload FROM outbox.outbox_events "
                    "WHERE event_type = 'PRODUCTION_ROUTE_SELECTED' "
                    "AND payload->>'route_id' = :rid"
                ),
                {"rid": route_id},
            )
        ).scalar_one_or_none()
    assert row is not None
    assert row["project_id"] == "proj_genroute_001"
    assert row["shot_id"] == "SHOT_001"


async def test_unverified_route_rejected(
    auth_client: AsyncClient,
    gen_sm: async_sessionmaker[AsyncSession],
) -> None:
    """ACT-ROUTE-NEG-001：UNVERIFIED 路线拒绝。"""
    route_id = f"route_{uuid.uuid4().hex[:8]}"
    await _seed_route(gen_sm, route_id=route_id, status=ProductionRouteStatus.UNVERIFIED)

    resp = await auth_client.post(
        "/actions/production.route.select",
        headers=_auth_headers(),
        json={
            "route_id": route_id,
            "project_id": "proj_genroute_001",
            "shot_id": "SHOT_001",
        },
    )
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "VALIDATION_ERROR"


async def test_ghost_route_not_found(auth_client: AsyncClient) -> None:
    resp = await auth_client.post(
        "/actions/production.route.select",
        headers=_auth_headers(),
        json={
            "route_id": "route_missing",
            "project_id": "proj_genroute_001",
            "shot_id": "SHOT_001",
        },
    )
    assert resp.status_code == 404
    assert resp.json()["error_code"] == "NOT_FOUND"


async def test_db_guard_blocks_unregistered_transition(
    gen_sm: async_sessionmaker[AsyncSession],
) -> None:
    """ADR-R20：直连 SQL 的非法转移在数据库层被拒（不经服务层也拦得住）。

    VERIFIED 无出边（终态）：尝试反向标回 UNVERIFIED 必须被转移表守卫拒绝。
    """
    route_id = f"route_{uuid.uuid4().hex[:8]}"
    await _seed_route(gen_sm, route_id=route_id, status=ProductionRouteStatus.VERIFIED)
    with pytest.raises(DBAPIError) as exc_info:
        async with gen_sm() as session:
            await set_raw_rls_context(
                session, tenant_id="00000000-0000-0000-0000-000000000000"
            )
            await session.execute(
                text(
                    "UPDATE generation_execution.production_routes "
                    "SET status = 'UNVERIFIED' "
                    "WHERE entity_id = :rid"
                ),
                {"rid": route_id},
            )
    assert "非法状态转移" in str(exc_info.value)


async def test_generation_role_cannot_write_shot_tables(
    gen_sm: async_sessionmaker[AsyncSession],
) -> None:
    """跨域角色隔离：generation 域角色写 shot_planning 表 → permission denied。"""
    with pytest.raises(DBAPIError) as exc_info:
        async with gen_sm() as session:
            await set_raw_rls_context(
                session, tenant_id="00000000-0000-0000-0000-000000000000"
            )
            await session.execute(
                text(
                    """
                    INSERT INTO shot_planning.shot_drafts
                        (entity_id, entity_type, schema_version, entity_version, status,
                         owner_module, tenant_id, organization_id, created_at,
                         created_by, updated_at, project_id, episode_ref, scene_ref,
                         shot_code, intent_summary, shot_description, source)
                    VALUES
                        ('x', 'ShotDraft', 205, 1, 'DRAFT', 'generation_execution', :t,
                         :t, now(), 'probe', now(), 'p1', 'EP1', 'SC1', 'S1',
                         '越权', '越权', 'agent')
                    """
                ),
                {"t": uuid.uuid4()},
            )
    message = str(exc_info.value).lower()
    assert "permission denied" in message or "insufficient privilege" in message
