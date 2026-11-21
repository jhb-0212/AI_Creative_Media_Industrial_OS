"""并发确认：每个 Shot 恰有一个 ACTIVE 修订（ADR-R17，部分唯一索引的真实行为）。

为什么需要真实并发用例
------------------------------------------------------------
``ux_shot_active_revision`` 是 ``(shot_id) WHERE status = 'ACTIVE'`` 的部分唯一索引。
它是不是「真的挡住了第二个 ACTIVE」，取决于两件事：索引在库里存在（静态可查），
以及**两个事务真的会在索引上相遇**（只有并发才能证明）。

串行插入只能证明「已经提交了一行 ACTIVE 后再插会被拒」。那条路径掩盖了真正的
风险：两个确认请求同时到达时，各自先 SELECT 到「没有 ACTIVE」，再各自 INSERT。
应用层的「先查后写」在这一刻必然失效，唯一能救的是数据库的索引——而它只在
未提交的冲突行上生效，也就是只在这个时序里生效。

本用例把那个时序显式构造出来：

1. 事务 A 插入 ACTIVE 修订，**不提交**；
2. 事务 B 插入同一 Shot 的 ACTIVE 修订 → 必须阻塞（A 的索引项未提交）；
3. A 提交；
4. B 醒来 → 唯一冲突（23505），而不是写进第二行。

若索引不存在或写成普通索引，第 4 步会成功写入两行 ACTIVE——这正是要拦的失效。
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from tests.conftest import set_test_scope
from tests.integration.planning_seed import (
    SHOT_REVISION_INSERT,
    seed_episode,
    seed_project,
    seed_scene,
    seed_shot,
    shot_revision_params,
)

# PostgreSQL 唯一约束冲突。
UNIQUE_VIOLATION = "23505"


def _sqlstate(exc: BaseException) -> str:
    orig = getattr(exc, "orig", exc)
    return getattr(orig, "sqlstate", "") or ""


async def _seed_chain(admin_sessionmaker, *, tenant, suffix: str):
    """播一条 项目 → 集数 → 场次 → 镜头 的链，返回四个 id。"""
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
    return project_id, episode_id, scene_id, shot_id


async def test_concurrent_active_revision_allows_exactly_one(
    db_sessionmaker, admin_sessionmaker
) -> None:
    """两个事务同时确认 → 恰一个 ACTIVE 落库，另一个撞唯一索引。"""
    tenant = uuid.uuid4()
    suffix = uuid.uuid4().hex[:8]
    project_id, episode_id, scene_id, shot_id = await _seed_chain(
        admin_sessionmaker, tenant=tenant, suffix=suffix
    )

    first_id = f"rev_a_{suffix}"
    second_id = f"rev_b_{suffix}"

    async with db_sessionmaker() as session_a, db_sessionmaker() as session_b:
        await set_test_scope(session_a, tenant_id=tenant)
        await set_test_scope(session_b, tenant_id=tenant)

        # 1. A 插入 ACTIVE，不提交（索引项对 B 可见但未定型）。
        await session_a.execute(
            text(SHOT_REVISION_INSERT),
            shot_revision_params(
                entity_id=first_id,
                shot_id=shot_id,
                project_id=project_id,
                episode_id=episode_id,
                scene_id=scene_id,
                tenant=tenant,
                revision_no=1,
            ),
        )

        # B 的锁等待设上限：真卡住时应报「等锁超时」，而不是让测试挂死。
        await session_b.execute(text("SET LOCAL lock_timeout = '10s'"))

        # 2. B 插入同一 Shot 的 ACTIVE：被 A 的未提交索引项挡住。
        pending = asyncio.create_task(
            session_b.execute(
                text(SHOT_REVISION_INSERT),
                shot_revision_params(
                    entity_id=second_id,
                    shot_id=shot_id,
                    project_id=project_id,
                    episode_id=episode_id,
                    scene_id=scene_id,
                    tenant=tenant,
                    revision_no=2,
                ),
            )
        )
        # 让 B 至少跑到「提交给数据库」这一步；它此后应当阻塞。
        await asyncio.sleep(0.2)
        assert not pending.done(), (
            "第二个 ACTIVE 插入没有被阻塞：部分唯一索引不存在（或不是唯一索引），"
            "并发确认会写入两条 ACTIVE 修订"
        )

        # 3. A 提交 → 冲突行定型。
        await session_a.commit()

        # 4. B 醒来必须撞唯一约束，而不是成功写入。
        with pytest.raises(IntegrityError) as exc_info:
            await pending
        assert _sqlstate(exc_info.value) == UNIQUE_VIOLATION, (
            f"并发插入的失败形态不是唯一冲突（SQLSTATE={_sqlstate(exc_info.value)}）"
        )
        await session_b.rollback()

    # 终态：库里恰有一行 ACTIVE。
    async with admin_sessionmaker() as session:
        active_count = await session.scalar(
            text(
                "SELECT count(*) FROM project_planning.shot_revisions "
                "WHERE shot_id = :shot AND status = 'ACTIVE'"
            ),
            {"shot": shot_id},
        )
    assert active_count == 1, f"ACTIVE 修订数应为 1，实际 {active_count}"
