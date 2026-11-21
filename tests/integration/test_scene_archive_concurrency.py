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
# 轮询 pg_stat_activity 等锁证据的上限（不是「睡一会儿看协程完没完」）。
_LOCK_WAIT_TIMEOUT = 5.0
_LOCK_WAIT_POLL = 0.02


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


async def _backend_pid(session) -> int:
    """取某个 session 的 PG 后端 pid，供 pg_stat_activity 精确指认。"""
    return int((await session.execute(text("SELECT pg_backend_pid()"))).scalar_one())


async def _assert_waiting_on_lock(admin_sessionmaker, pid: int, label: str) -> None:
    """确认目标后端**真的进入了锁等待**（第二轮审查 P2-03）。

    「协程还没 done」只证明它没跑完，可能只是还没被调度到数据库语句——那样
    本用例证明的是「asyncio 调度」，不是「PG 行锁互斥」。这里从 pg_stat_activity
    实读 wait_event_type='Lock'，拿到数据库级证据才放行。
    """
    import time

    deadline = time.monotonic() + _LOCK_WAIT_TIMEOUT
    async with admin_sessionmaker() as observer:
        while time.monotonic() < deadline:
            row = (
                await observer.execute(
                    text(
                        "SELECT wait_event_type, wait_event FROM pg_stat_activity "
                        "WHERE pid = :pid"
                    ),
                    {"pid": pid},
                )
            ).first()
            if row is not None and row[0] == "Lock":
                return  # 真实锁等待证据到手
            await asyncio.sleep(_LOCK_WAIT_POLL)
    raise AssertionError(
        f"{label}（pid={pid}）在 {_LOCK_WAIT_TIMEOUT}s 内未进入 pg_stat_activity 的 Lock 等待；"
        "协程未完成不等于数据库在等锁，本用例无法证明行锁互斥"
    )


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
        blocked_pid = await _backend_pid(session_b)
        pending = asyncio.create_task(
            SqlAlchemySceneRepository(session_b).lock_for_child_write(scene_id)
        )
        # 等真实锁证据，而不是「睡 0.2s 看协程完没完」。
        await _assert_waiting_on_lock(
            admin_sessionmaker, blocked_pid, "子对象侧共享锁"
        )
        assert not pending.done(), "已进入锁等待却仍完成：等锁语义异常"

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
        blocked_pid = await _backend_pid(session_b)
        pending = asyncio.create_task(
            SqlAlchemySceneRepository(session_b).lock_for_archive(scene_id)
        )
        await _assert_waiting_on_lock(admin_sessionmaker, blocked_pid, "归档方排他锁")
        assert not pending.done(), "已进入锁等待却仍完成：等锁语义异常"

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
