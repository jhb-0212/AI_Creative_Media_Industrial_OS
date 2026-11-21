"""SC-5 的并发语义：归档与「进入在产」必须在同一行上串行化（ADR-R40 §6.3）。

为什么静态闸门与本用例都不能少
------------------------------------------------------------
「先查后写」这类缺陷在单线程测试里永远全绿：查询与写入之间没有第二个事务，
中间那个窗口不存在。它只在一个特定时序里显形——

    T1: 锁父场次 → 判定「无在产镜头」（成立）
    T2: 把某镜头推入在产并提交（发生在上一步之后）
    T1: 写 ARCHIVED 并提交            ← 库里出现「已归档场次带着在产镜头」

于是必须把两个方向都构造出来：先归档、先进产，各验一次「对方确实被挡住」。
只验一个方向会漏掉另一侧的锁模式写错（例如子侧渲染成 ``FOR NO KEY UPDATE``，
它与自身不冲突，屏障一次都没挡住）。

断言只落在**锁的互斥**与**锁内可见的状态**上，不去断言具体错误码：错误码是
服务层的事，已由 ``tests/unit/test_scene_service.py`` 与
``tests/unit/test_shot_service.py`` 覆盖。这一层的价值是证明那两条语句真的会在
数据库里相遇。
"""

from __future__ import annotations

import asyncio
import uuid

from sqlalchemy import text

from services.project_planning.domain.enums import SceneStatus, ShotStatus
from services.project_planning.repository.shot_planning_sqlalchemy_repos import (
    SqlAlchemySceneRepository,
    SqlAlchemyShotRepository,
)
from tests.conftest import set_test_scope
from tests.integration.planning_seed import (
    seed_episode,
    seed_project,
    seed_scene,
    seed_shot,
)

# 等锁上限：真卡住时应报「等锁超时」，而不是让测试挂死。
_LOCK_TIMEOUT = "10s"
# 让被阻塞的协程跑到「提交给数据库」这一步再断言它没完成。
_SETTLE_SECONDS = 0.2


async def _seed(admin_sessionmaker, *, tenant, suffix: str):
    project_id = f"proj_{suffix}"
    episode_id = f"ep_{suffix}"
    scene_id = f"scene_{suffix}"
    shot_id = f"shot_{suffix}"
    await seed_project(admin_sessionmaker, project_id=project_id, tenant=tenant)
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
    await seed_shot(
        admin_sessionmaker,
        shot_id=shot_id,
        scene_id=scene_id,
        project_id=project_id,
        tenant=tenant,
    )
    return scene_id, shot_id


async def test_archiving_blocks_a_concurrent_entering_of_production(
    db_sessionmaker, admin_sessionmaker
) -> None:
    """归档方持排他锁期间，子对象侧取不到共享锁；解锁后看到的是已归档。"""
    tenant = uuid.uuid4()
    scene_id, _ = await _seed(
        admin_sessionmaker, tenant=tenant, suffix=uuid.uuid4().hex[:8]
    )

    async with db_sessionmaker() as session_a, db_sessionmaker() as session_b:
        await set_test_scope(session_a, tenant_id=tenant)
        await set_test_scope(session_b, tenant_id=tenant)
        scenes_a = SqlAlchemySceneRepository(session_a)

        # 1. 归档方：锁父行（与 SceneService 归档分支同一条语句）。
        locked = await scenes_a.lock_for_archive(scene_id)
        assert locked is not None and locked.status is SceneStatus.DRAFT

        # 2. 归档方在锁内确认「其下无在产镜头」，与服务的判定同一条语句。
        assert not await SqlAlchemyShotRepository(session_a).has_with_status(
            scene_id, ShotStatus.IN_PRODUCTION
        )
        # 3. 归档方推进状态但**不提交**。
        locked.status = SceneStatus.ARCHIVED
        await scenes_a.save(locked, locked.entity_version)

        # 4. 子对象侧：进入在产前的共享锁必须被挡住。
        await session_b.execute(text(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'"))
        pending = asyncio.create_task(
            SqlAlchemySceneRepository(session_b).lock_for_child_write(scene_id)
        )
        await asyncio.sleep(_SETTLE_SECONDS)
        assert not pending.done(), (
            "子对象侧的共享锁没有被归档方的排他锁挡住："
            "「先查后写」的窗口在并发下会写入「已归档场次 + 在产镜头」"
        )

        # 5. 归档提交 → 子对象侧醒来，必须看到已归档（EvalPlanQual 后读到新版本）。
        await session_a.commit()
        observed = await pending
        assert observed is not None
        assert observed.status is SceneStatus.ARCHIVED, (
            "锁内读到的仍是归档前的版本：判定会基于过期数据放行"
        )
        await session_b.rollback()


async def test_entering_production_blocks_a_concurrent_archive(
    db_sessionmaker, admin_sessionmaker
) -> None:
    """反向：镜头侧持共享锁期间，归档方取不到排他锁；解锁后能看到在产镜头。"""
    tenant = uuid.uuid4()
    scene_id, shot_id = await _seed(
        admin_sessionmaker, tenant=tenant, suffix=uuid.uuid4().hex[:8]
    )

    async with db_sessionmaker() as session_a, db_sessionmaker() as session_b:
        await set_test_scope(session_a, tenant_id=tenant)
        await set_test_scope(session_b, tenant_id=tenant)
        shots_a = SqlAlchemyShotRepository(session_a)

        # 1. 镜头方：先取父行共享锁（与 ShotService 进产分支同一条语句）。
        parent = await SqlAlchemySceneRepository(session_a).lock_for_child_write(scene_id)
        assert parent is not None and parent.status is not SceneStatus.ARCHIVED

        shot = await shots_a.get_by_id(shot_id)
        assert shot is not None
        shot.status = ShotStatus.IN_PRODUCTION
        await shots_a.save(shot, shot.entity_version)

        # 2. 归档方：排他锁必须等待，而不是直接通过。
        await session_b.execute(text(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'"))
        pending = asyncio.create_task(
            SqlAlchemySceneRepository(session_b).lock_for_archive(scene_id)
        )
        await asyncio.sleep(_SETTLE_SECONDS)
        assert not pending.done(), (
            "归档方的排他锁没有被镜头侧的共享锁挡住："
            "归档可以在「镜头刚进在产」的瞬间通过"
        )

        # 3. 镜头方提交 → 归档方醒来，必须在锁内看到在产镜头并因此拒绝归档。
        await session_a.commit()
        await pending
        in_production = await SqlAlchemyShotRepository(session_b).has_with_status(
            scene_id, ShotStatus.IN_PRODUCTION
        )
        assert in_production is True, (
            "归档方解锁后仍看不到已提交的在产镜头：判定会放行非法归档"
        )
        await session_b.rollback()
