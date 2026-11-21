"""A13 五张新表的行级安全：跨租户/跨组织不可读，越界写入被拒（SCOPE-4 / R8）。

为什么这份文件必须存在
------------------------------------------------------------
A13 初版建了五张表、配了授权、下沉了约束，但**一条 RLS 策略都没加**。库层因此
完全敞开：``project_planning_role`` 在任意上下文里都能读出全部租户、全部组织的
场次与镜头。当时静态闸门是绿的——因为组织边界断言只对它自己那份手写表清单求值，
新表不在清单里（判据侧已改为从真实矩阵派生应检集合）。

判据改对只证明「判据会报红」，本文件补的是另一半：**策略在真实 PostgreSQL 上
确实过滤行**。三层各证一件事，缺一层就有一层的证据是注释：

1. ``pg_class.relrowsecurity`` / ``relforcerowsecurity`` —— 策略与 FORCE 真的挂在表上；
2. 跨租户、跨组织读为 0，本作用域读为 1 —— 谓词真的过滤行（不是只写在 DDL 里）；
3. 跨租户写入被拒（42501）—— ``WITH CHECK`` 也生效，而不只是查询被过滤。

依赖真实 PostgreSQL；无 PG 环境时随其余集成测试一起失败在连接上（CI 有 PG 时执行）。
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError

from tests.conftest import set_test_scope
from tests.integration.planning_seed import (
    SCENE_INSERT,
    now,
    seed_episode,
    seed_organization,
    seed_project,
    seed_scene,
)

# RLS 拒绝的 SQLSTATE（行级安全策略的 WITH CHECK 不通过）。
RLS_DENIAL = "42501"

_NEW_TABLES = (
    "scenes",
    "shots",
    "shot_revisions",
    "shot_breakdown_drafts",
    "rework_tasks",
)


def _sqlstate(exc: BaseException) -> str:
    """取驱动 SQLSTATE（SQLAlchemy 把原始 asyncpg 异常放在 ``orig`` 上）。"""
    orig = getattr(exc, "orig", exc)
    return getattr(orig, "sqlstate", "") or ""


async def _count_scene(session, scene_id: str) -> int:
    return (
        await session.scalar(
            text(
                "SELECT count(*) FROM project_planning.scenes WHERE entity_id = :eid"
            ),
            {"eid": scene_id},
        )
        or 0
    )


async def test_new_tables_have_rls_enabled_and_forced(admin_sessionmaker) -> None:
    """五张新表必须 ``ENABLE`` 且 ``FORCE`` 行级安全。

    只查 CREATE POLICY 不够：策略存在但表没 ENABLE 时，它是**惰性**的——DDL 里
    有、库里没有。这一条直接读 catalogs，回答「数据库当前真的开没开」。
    """
    async with admin_sessionmaker() as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity
                    FROM pg_class c
                    JOIN pg_namespace n ON n.oid = c.relnamespace
                    WHERE n.nspname = 'project_planning'
                      AND c.relname = ANY(CAST(:tables AS text[]))
                    """
                ),
                {"tables": list(_NEW_TABLES)},
            )
        ).all()

    flags = {name: (enabled, forced) for name, enabled, forced in rows}
    assert set(flags) == set(_NEW_TABLES), f"表缺失：{set(_NEW_TABLES) - set(flags)}"
    assert all(enabled for enabled, _ in flags.values()), (
        f"未启用 RLS（策略会是惰性的）：{flags}"
    )
    assert all(forced for _, forced in flags.values()), f"未 FORCE RLS：{flags}"


async def test_scenes_are_invisible_across_tenants(
    db_sessionmaker, admin_sessionmaker
) -> None:
    """跨租户读为 0、本租户读为 1（租户维度的运行时证据）。"""
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    project_id = f"proj_{tenant_a.hex[:8]}"
    episode_id = f"ep_{tenant_a.hex[:8]}"
    scene_id = f"scene_{tenant_a.hex[:8]}"

    await seed_project(admin_sessionmaker, project_id=project_id, tenant=tenant_a)
    await seed_episode(
        admin_sessionmaker, episode_id=episode_id, project_id=project_id, tenant=tenant_a
    )
    await seed_scene(
        admin_sessionmaker,
        scene_id=scene_id,
        project_id=project_id,
        episode_id=episode_id,
        tenant=tenant_a,
    )

    async with db_sessionmaker() as session:
        await set_test_scope(session, tenant_id=tenant_a)
        assert await _count_scene(session, scene_id) == 1

        await set_test_scope(session, tenant_id=tenant_b)
        assert await _count_scene(session, scene_id) == 0, (
            "跨租户读到了场次：策略没过滤行（或项目未加策略）"
        )
        await session.rollback()


async def test_scenes_are_invisible_across_organizations(
    db_sessionmaker, admin_sessionmaker
) -> None:
    """同租户、不同组织读为 0（组织维度经父项目存在性传递）。

    子表不存 ``organization_id``：组织规则由 ``project_planning.projects`` 自己的
    策略在 EXISTS 子查询内执行。这条用例证明「传递」真的成立——如果子表只抄了
    租户谓词、没抄父项目存在性，这里会读到 1。
    """
    tenant = uuid.uuid4()
    org_a = uuid.uuid4()
    org_b = uuid.uuid4()
    project_id = f"proj_org_{tenant.hex[:8]}"
    episode_id = f"ep_org_{tenant.hex[:8]}"
    scene_id = f"scene_org_{tenant.hex[:8]}"

    await seed_organization(admin_sessionmaker, org_id=org_a, tenant=tenant)
    await seed_project(
        admin_sessionmaker, project_id=project_id, tenant=tenant, org_id=org_a
    )
    await seed_episode(
        admin_sessionmaker, episode_id=episode_id, project_id=project_id, tenant=tenant
    )
    await seed_scene(
        admin_sessionmaker,
        scene_id=scene_id,
        project_id=project_id,
        episode_id=episode_id,
        tenant=tenant,
    )

    async with db_sessionmaker() as session:
        await set_test_scope(session, org_a, tenant_id=tenant)
        assert await _count_scene(session, scene_id) == 1

        await set_test_scope(session, org_b, tenant_id=tenant)
        assert await _count_scene(session, scene_id) == 0, (
            "同租户的他组织读到了场次：子表策略未用父项目存在性表达组织规则"
        )
        await session.rollback()


async def test_cross_tenant_scene_insert_is_rejected(
    db_sessionmaker, admin_sessionmaker
) -> None:
    """向「自己租户 + 他租户项目」写入场次 → ``WITH CHECK`` 拒绝（42501）。

    只测读会让「策略只过滤 SELECT」的写法过关：``CREATE POLICY ... FOR SELECT``
    就是那样，读不到却写得进。``USING`` 在默认 ``FOR ALL`` 策略下同时充当
    ``WITH CHECK``，这条用例证明它真的在拦写。
    """
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    project_id = f"proj_w_{tenant_a.hex[:8]}"
    episode_id = f"ep_w_{tenant_a.hex[:8]}"

    await seed_project(admin_sessionmaker, project_id=project_id, tenant=tenant_a)
    await seed_episode(
        admin_sessionmaker, episode_id=episode_id, project_id=project_id, tenant=tenant_a
    )

    intruder_id = f"scene_w_{tenant_a.hex[:8]}"
    async with db_sessionmaker() as session:
        await set_test_scope(session, tenant_id=tenant_b)
        with pytest.raises(ProgrammingError) as exc_info:
            await session.execute(
                text(SCENE_INSERT),
                {
                    "eid": intruder_id,
                    "tid": tenant_b,
                    "now": now(),
                    "code": "SC99",
                    "pid": project_id,
                    "epid": episode_id,
                },
            )
        assert _sqlstate(exc_info.value) == RLS_DENIAL, (
            f"跨租户写入未被 RLS 拒绝（SQLSTATE={_sqlstate(exc_info.value)}）"
        )
        await session.rollback()
