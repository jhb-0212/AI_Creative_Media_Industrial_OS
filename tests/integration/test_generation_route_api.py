"""Generation Route API 集成测试（异步，连真实 PostgreSQL，走真实鉴权）。

验证 PRODUCTION.ROUTE.SELECT 主链入口、generation_execution 域边界，以及
Phase 1 验收 C4「单一路线生成」（V205-R14-A9）：

- VERIFIED 路线选中成功：写下生效路线 + 落审计事件（outbox，含 selection_id）；
- 同路线重复选择幂等：不新增记录、不发第二条事件；
- 第二条路线拒绝（ROUTE_ALREADY_SELECTED，409，details 结构化）；
- **并发**两条路线：唯一约束裁决，恰好一个 200、一个 409，库里只有一条记录。
  C4 的验收口径就是这一条——并发的第二条不得"静默排队"或"自动改路"；
- UNVERIFIED 路线拒绝（ACT-ROUTE-NEG-001，422）；幽灵路线 404；
- 状态机守卫触发器在数据库层拦截非法转移（ADR-R20）；
- 生效路线不可改写、不可删除（受理事实，触发器 + 权限双闸）；
- 跨域角色隔离：generation 域角色写不了 shot_planning 表，反之亦然。

每个用例使用**唯一的 project_id / shot_id**：生效路线的唯一性跨测试运行持续
有效（表只增不改），复用固定 id 会让第二次运行从"选路成功"变成"409"——
那会让测试结果依赖数据库历史，而不是代码行为。
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from packages.shared.database import build_migration_url
from services.generation_execution.domain.enums import ProductionRouteStatus
from services.generation_execution.domain.production_route import ProductionRoute
from services.generation_execution.repository.production_route_sqlalchemy_repo import (
    SqlAlchemyProductionRouteRepository,
)
from tests.conftest import make_test_headers, set_raw_rls_context

pytestmark = pytest.mark.asyncio

_TENANT = "00000000-0000-0000-0000-000000000000"


@pytest.fixture(scope="module")
async def admin_engine() -> AsyncEngine:
    """以迁移角色（超级用户）连库：用于证明触发器对特权会话同样生效。"""
    engine = create_async_engine(build_migration_url(), pool_pre_ping=True)
    try:
        yield engine
    finally:
        await engine.dispose()


def _auth_headers(subject_id: str = "producer_001") -> dict[str, str]:
    return make_test_headers(subject_id=subject_id, roles=["制片"])


def _new_scope() -> tuple[str, str]:
    """唯一 (project_id, shot_id)：避免与库中历史生效路线冲突。"""
    return f"proj_genroute_{uuid.uuid4().hex[:8]}", f"SHOT_{uuid.uuid4().hex[:6]}"


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
        tenant_id=uuid.UUID(_TENANT),
    )
    async with gen_sm() as session:
        await set_raw_rls_context(session, tenant_id=_TENANT)
        await SqlAlchemyProductionRouteRepository(session).add(route)
        await session.commit()


async def _select(
    client: AsyncClient, *, route_id: str, project_id: str, shot_id: str
) -> object:
    return await client.post(
        "/actions/production.route.select",
        headers=_auth_headers(),
        json={
            "route_id": route_id,
            "project_id": project_id,
            "shot_id": shot_id,
        },
    )


async def _count_events(
    gen_sm: async_sessionmaker[AsyncSession], *, project_id: str, shot_id: str
) -> int:
    async with gen_sm() as session:
        await set_raw_rls_context(session, tenant_id=_TENANT)
        count = (
            await session.execute(
                text(
                    "SELECT count(*) FROM outbox.outbox_events "
                    "WHERE event_type = 'PRODUCTION_ROUTE_SELECTED' "
                    "AND payload->>'project_id' = :pid "
                    "AND payload->>'shot_id' = :sid"
                ),
                {"pid": project_id, "sid": shot_id},
            )
        ).scalar_one()
    return int(count)


async def _count_selections(
    gen_sm: async_sessionmaker[AsyncSession], *, project_id: str, shot_id: str
) -> int:
    async with gen_sm() as session:
        await set_raw_rls_context(session, tenant_id=_TENANT)
        count = (
            await session.execute(
                text(
                    "SELECT count(*) FROM generation_execution.shot_route_selections "
                    "WHERE project_id = :pid AND shot_id = :sid"
                ),
                {"pid": project_id, "sid": shot_id},
            )
        ).scalar_one()
    return int(count)


async def test_select_verified_route_writes_effective_route(
    auth_client: AsyncClient,
    gen_sm: async_sessionmaker[AsyncSession],
) -> None:
    route_id = f"route_{uuid.uuid4().hex[:8]}"
    await _seed_route(gen_sm, route_id=route_id)
    project_id, shot_id = _new_scope()

    resp = await _select(
        auth_client, route_id=route_id, project_id=project_id, shot_id=shot_id
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ACCEPTED"
    assert body["audit_event"] == "PRODUCTION_ROUTE_SELECTED"
    assert body["route_id"] == route_id
    assert body["selection_id"].startswith("srs_")
    assert body["idempotent_replay"] is False

    # 生效路线是结构事实：库里确有其行（不是"只发了个事件"）。
    assert await _count_selections(gen_sm, project_id=project_id, shot_id=shot_id) == 1
    async with gen_sm() as session:
        await set_raw_rls_context(session, tenant_id=_TENANT)
        row = (
            await session.execute(
                text(
                    "SELECT payload FROM outbox.outbox_events "
                    "WHERE event_type = 'PRODUCTION_ROUTE_SELECTED' "
                    "AND payload->>'selection_id' = :sid"
                ),
                {"sid": body["selection_id"]},
            )
        ).scalar_one_or_none()
    assert row is not None
    assert row["route_id"] == route_id
    assert row["project_id"] == project_id
    assert row["shot_id"] == shot_id


async def test_reselect_same_route_is_idempotent(
    auth_client: AsyncClient,
    gen_sm: async_sessionmaker[AsyncSession],
) -> None:
    """D3 幂等：同一路线重复选择不产生第二条记录、第二条事件。"""
    route_id = f"route_{uuid.uuid4().hex[:8]}"
    await _seed_route(gen_sm, route_id=route_id)
    project_id, shot_id = _new_scope()

    first = await _select(
        auth_client, route_id=route_id, project_id=project_id, shot_id=shot_id
    )
    assert first.status_code == 200, first.text
    second = await _select(
        auth_client, route_id=route_id, project_id=project_id, shot_id=shot_id
    )
    assert second.status_code == 200, second.text
    assert second.json()["selection_id"] == first.json()["selection_id"]
    assert second.json()["idempotent_replay"] is True

    assert await _count_selections(gen_sm, project_id=project_id, shot_id=shot_id) == 1
    assert await _count_events(gen_sm, project_id=project_id, shot_id=shot_id) == 1


async def test_second_route_rejected_with_structured_error(
    auth_client: AsyncClient,
    gen_sm: async_sessionmaker[AsyncSession],
) -> None:
    """C4：已选路线后再选另一条 → 409 ROUTE_ALREADY_SELECTED（不静默排队）。"""
    first_route = f"route_{uuid.uuid4().hex[:8]}"
    second_route = f"route_{uuid.uuid4().hex[:8]}"
    await _seed_route(gen_sm, route_id=first_route)
    await _seed_route(gen_sm, route_id=second_route)
    project_id, shot_id = _new_scope()

    first = await _select(
        auth_client, route_id=first_route, project_id=project_id, shot_id=shot_id
    )
    assert first.status_code == 200, first.text

    second = await _select(
        auth_client, route_id=second_route, project_id=project_id, shot_id=shot_id
    )
    assert second.status_code == 409, second.text
    payload = second.json()
    assert payload["error_code"] == "ROUTE_ALREADY_SELECTED"
    assert payload["category"] == "CONFLICT"
    assert payload["retryable"] is False
    # 结构化 details：调用方要能据此知道"被哪条路线、哪次选择挡住"。
    details = payload["details"]
    assert details["effective_route_id"] == first_route
    assert details["effective_selection_id"] == first.json()["selection_id"]
    assert details["requested_route_id"] == second_route

    # 拒绝无副作用：仍只有一条生效路线、一条审计事件。
    assert await _count_selections(gen_sm, project_id=project_id, shot_id=shot_id) == 1
    assert await _count_events(gen_sm, project_id=project_id, shot_id=shot_id) == 1


async def test_concurrent_two_routes_exactly_one_wins(
    auth_client: AsyncClient,
    gen_sm: async_sessionmaker[AsyncSession],
) -> None:
    """C4 核心证据：并发发起两条路线，恰好一条成功、另一条 409。

    两个请求在选路前的"查有没有生效路线"都会查到"没有"——这正是应用层
    查重必输的场景。裁决者是数据库唯一约束；服务层只负责把失败翻译成
    结构化拒绝。若这里出现两个 200，就说明"单一路线生成"只存在于文档里。
    """
    route_a = f"route_{uuid.uuid4().hex[:8]}"
    route_b = f"route_{uuid.uuid4().hex[:8]}"
    await _seed_route(gen_sm, route_id=route_a)
    await _seed_route(gen_sm, route_id=route_b)
    project_id, shot_id = _new_scope()

    responses = await asyncio.gather(
        _select(auth_client, route_id=route_a, project_id=project_id, shot_id=shot_id),
        _select(auth_client, route_id=route_b, project_id=project_id, shot_id=shot_id),
    )
    statuses = sorted(r.status_code for r in responses)
    assert statuses == [200, 409], [r.text for r in responses]

    winner = next(r for r in responses if r.status_code == 200)
    loser = next(r for r in responses if r.status_code == 409)
    assert loser.json()["error_code"] == "ROUTE_ALREADY_SELECTED"
    assert loser.json()["details"]["effective_route_id"] == winner.json()["route_id"]

    # 结构不变量：一条生效路线、一条审计事件（"输掉的那次"没有留下任何事实）。
    assert await _count_selections(gen_sm, project_id=project_id, shot_id=shot_id) == 1
    assert await _count_events(gen_sm, project_id=project_id, shot_id=shot_id) == 1


async def test_unique_constraint_rejects_second_row_without_service(
    gen_sm: async_sessionmaker[AsyncSession],
) -> None:
    """C4 的结构证据：唯一约束独立于服务层存在（直连 SQL 也插不进第二条）。

    并发用例证明"并发时的结果"，本用例证明"为什么并发时会有这个结果"——
    如果哪天有人把唯一约束从迁移里删掉、只留服务层查重，并发用例可能因为
    时序巧合仍然偶尔通过，而这条会稳定变红。
    """
    project_id, shot_id = _new_scope()
    base = {
        "entity_id": f"srs_{uuid.uuid4().hex[:8]}",
        "tenant_id": uuid.UUID(_TENANT),
        "project_id": project_id,
        "shot_id": shot_id,
    }
    insert = (
        "INSERT INTO generation_execution.shot_route_selections "
        "(entity_id, entity_type, schema_version, entity_version, status, "
        " owner_module, tenant_id, organization_id, created_at, created_by, "
        " updated_at, audit_ref, project_id, shot_id, route_id) "
        "VALUES (:entity_id, 'ShotRouteSelection', 205, 1, 'ACTIVE', "
        " 'generation-execution', :tenant_id, :tenant_id, now(), 'probe', "
        " now(), NULL, :project_id, :shot_id, :route_id)"
    )
    async with gen_sm() as session:
        await set_raw_rls_context(session, tenant_id=_TENANT)
        await session.execute(
            text(insert), {**base, "route_id": "route_first"}
        )
        await session.commit()

    with pytest.raises(DBAPIError) as exc_info:
        async with gen_sm() as session:
            await set_raw_rls_context(session, tenant_id=_TENANT)
            await session.execute(
                text(insert),
                {
                    **base,
                    "entity_id": f"srs_{uuid.uuid4().hex[:8]}",
                    "route_id": "route_second",
                },
            )
    message = str(exc_info.value)
    assert "ux_shot_route_selection_effective" in message
    assert await _count_selections(gen_sm, project_id=project_id, shot_id=shot_id) == 1


async def test_effective_route_cannot_be_rewritten_or_deleted(
    auth_client: AsyncClient,
    gen_sm: async_sessionmaker[AsyncSession],
    admin_engine: AsyncEngine,
) -> None:
    """生效路线是受理事实：改写与删除都必须被拒。

    两道闸分别验证，不能只测其中一道：
    1. **权限层**：应用角色连 UPDATE/DELETE 权限都没有（第一道）；
    2. **触发器层**：迁移角色（超级用户）能越过权限，但仍被守卫函数拒绝
       （第二道）。只测权限层的话，任何一次误授 UPDATE 都会让"不可改写"
       悄悄失效，而测试仍然绿。
    """
    route_id = f"route_{uuid.uuid4().hex[:8]}"
    await _seed_route(gen_sm, route_id=route_id)
    project_id, shot_id = _new_scope()
    resp = await _select(
        auth_client, route_id=route_id, project_id=project_id, shot_id=shot_id
    )
    assert resp.status_code == 200, resp.text

    # 1) 应用角色：无权限。
    for statement in (
        "UPDATE generation_execution.shot_route_selections "
        "SET route_id = 'route_forged' WHERE project_id = :pid AND shot_id = :sid",
        "DELETE FROM generation_execution.shot_route_selections "
        "WHERE project_id = :pid AND shot_id = :sid",
    ):
        with pytest.raises(DBAPIError) as exc_info:
            async with gen_sm() as session:
                await set_raw_rls_context(session, tenant_id=_TENANT)
                await session.execute(
                    text(statement), {"pid": project_id, "sid": shot_id}
                )
        message = str(exc_info.value).lower()
        assert "permission denied" in message or "insufficient privilege" in message

    # 2) 迁移角色（超级用户）：权限不缺，触发器必须拦住。
    #    每个语句独立连接：守卫抛异常后事务已中止，复用同一连接只会拿到
    #    25P02（"当前事务已中止"），那测的是连接状态而不是守卫。
    async with admin_engine.connect() as conn:
        with pytest.raises(DBAPIError) as update_exc:
            await conn.execute(
                text(
                    "UPDATE generation_execution.shot_route_selections "
                    "SET route_id = 'route_forged' "
                    "WHERE project_id = :pid AND shot_id = :sid"
                ),
                {"pid": project_id, "sid": shot_id},
            )
        assert "禁止改写" in str(update_exc.value)

    async with admin_engine.connect() as conn:
        with pytest.raises(DBAPIError) as delete_exc:
            await conn.execute(
                text(
                    "DELETE FROM generation_execution.shot_route_selections "
                    "WHERE project_id = :pid AND shot_id = :sid"
                ),
                {"pid": project_id, "sid": shot_id},
            )
        assert "不可物理删除" in str(delete_exc.value)


async def test_unverified_route_rejected(
    auth_client: AsyncClient,
    gen_sm: async_sessionmaker[AsyncSession],
) -> None:
    """ACT-ROUTE-NEG-001：UNVERIFIED 路线拒绝。"""
    route_id = f"route_{uuid.uuid4().hex[:8]}"
    await _seed_route(gen_sm, route_id=route_id, status=ProductionRouteStatus.UNVERIFIED)
    project_id, shot_id = _new_scope()

    resp = await _select(
        auth_client, route_id=route_id, project_id=project_id, shot_id=shot_id
    )
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "VALIDATION_ERROR"
    assert await _count_selections(gen_sm, project_id=project_id, shot_id=shot_id) == 0


async def test_ghost_route_not_found(auth_client: AsyncClient) -> None:
    project_id, shot_id = _new_scope()
    resp = await _select(
        auth_client, route_id="route_missing", project_id=project_id, shot_id=shot_id
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
            await set_raw_rls_context(session, tenant_id=_TENANT)
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
            await set_raw_rls_context(session, tenant_id=_TENANT)
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
