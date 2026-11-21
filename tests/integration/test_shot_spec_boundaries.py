"""ShotSpec 的库层边界：RLS、跨项目绑定、SS-3 CHECK、一修订一规格（A22）。

为什么这些判据非得在真实 PostgreSQL 上跑
------------------------------------------------------------
领域层的 SS-3 与「绑定同项目」都能被绕过——运维脚本、后台补偿、将来的批处理
直连数据库写入时，领域对象根本不在场。只有库层能拦住它们，而「库层拦得住」
这句话必须由数据库本身回答，不能由服务层再来一遍校验来证明。

四条判据各自对应一种失效形态，且都不能被其他三条覆盖：

1. **跨租户读为 0**：策略真的过滤行（不是只写在 DDL 里）；
2. **跨项目绑定被拒 23503**：复合外键 ``(project_id, shot_revision_id)`` 真的
   成立——单列外键在这里会放过「规格属 A、修订属 B」，那正是 ACT-SPEC-NEG-002
   的形态；
3. **切点非单调 / 空数组被拒 23514**：SS-3 的库层形态，含「空数组使 CHECK 求值
   为 NULL 而放行」这个陷阱（函数内必须 ``COALESCE(..., false)``）；
4. **同一修订第二份规格被拒 23505**：唯一约束是唯一能真正拒绝并发的机制
   （服务层「先查后插」在并发下必被穿透）。

播种与负向写入一律走 admin 连接：要让失败原因可归因——用业务角色播种时，
RLS 拒绝与约束拒绝会混成同一种错误。
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from tests.conftest import set_test_scope
from tests.integration.planning_seed import (
    SHOT_REVISION_INSERT,
    SHOT_SPEC_INSERT,
    seed_episode,
    seed_project,
    seed_scene,
    seed_shot,
    seed_shot_spec,
    shot_revision_params,
    shot_spec_params,
)

# 唯一约束违反（同一修订第二份规格）。
UNIQUE_VIOLATION = "23505"
# 外键违反（跨项目绑定）。
FK_VIOLATION = "23503"
# CHECK 违反（SS-3 / 正数 / 状态枚举）。
CHECK_VIOLATION = "23514"


def _sqlstate(exc: BaseException) -> str:
    orig = getattr(exc, "orig", exc)
    return getattr(orig, "sqlstate", "") or ""


async def _chain(admin_sessionmaker, *, tag: str, tenant) -> dict[str, str]:
    """建「项目 → 集数 → 场次 → 镜头 → 修订」这条链，返回各级 id。"""
    ids = {
        "project": f"proj_{tag}",
        "episode": f"ep_{tag}",
        "scene": f"scene_{tag}",
        "shot": f"shot_{tag}",
        "revision": f"rev_{tag}",
    }
    await seed_project(admin_sessionmaker, project_id=ids["project"], tenant=tenant)
    await seed_episode(
        admin_sessionmaker,
        episode_id=ids["episode"],
        project_id=ids["project"],
        tenant=tenant,
    )
    await seed_scene(
        admin_sessionmaker,
        scene_id=ids["scene"],
        project_id=ids["project"],
        episode_id=ids["episode"],
        tenant=tenant,
        code=f"SC-{tag}",
    )
    await seed_shot(
        admin_sessionmaker,
        shot_id=ids["shot"],
        scene_id=ids["scene"],
        project_id=ids["project"],
        tenant=tenant,
        code=f"SH-{tag}",
    )
    async with admin_sessionmaker() as session:
        await session.execute(
            text(SHOT_REVISION_INSERT),
            shot_revision_params(
                entity_id=ids["revision"],
                shot_id=ids["shot"],
                project_id=ids["project"],
                episode_id=ids["episode"],
                scene_id=ids["scene"],
                tenant=tenant,
                revision_no=1,
            ),
        )
        await session.commit()
    return ids


async def test_shot_specs_are_invisible_across_tenants(
    db_sessionmaker, admin_sessionmaker
) -> None:
    """跨租户读为 0、本租户读为 1（组织维度经父项目存在性传递）。"""
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    ids = await _chain(admin_sessionmaker, tag=f"rls_{tenant_a.hex[:6]}", tenant=tenant_a)
    spec_id = f"spec_{tenant_a.hex[:6]}"
    await seed_shot_spec(
        admin_sessionmaker,
        entity_id=spec_id,
        project_id=ids["project"],
        shot_revision_id=ids["revision"],
        tenant=tenant_a,
    )

    async with db_sessionmaker() as session:
        await set_test_scope(session, tenant_id=tenant_a)
        own = await session.scalar(
            text("SELECT count(*) FROM project_planning.shot_specs WHERE entity_id = :eid"),
            {"eid": spec_id},
        )
        assert own == 1

        await set_test_scope(session, tenant_id=tenant_b)
        other = await session.scalar(
            text("SELECT count(*) FROM project_planning.shot_specs WHERE entity_id = :eid"),
            {"eid": spec_id},
        )
        assert other == 0, "跨租户读到了规格：策略没过滤行（或未 ENABLE/FORCE）"
        await session.rollback()


async def test_binding_to_a_revision_of_another_project_is_rejected(
    admin_sessionmaker,
) -> None:
    """``(project_id, shot_revision_id)`` 复合外键：单列外键会放过这种绑定。"""
    tenant = uuid.uuid4()
    tag_a = f"fka_{tenant.hex[:6]}"
    tag_b = f"fkb_{tenant.hex[:6]}"
    project_a = await _chain(admin_sessionmaker, tag=tag_a, tenant=tenant)
    project_b = await _chain(admin_sessionmaker, tag=tag_b, tenant=tenant)

    async with admin_sessionmaker() as session:
        with pytest.raises(IntegrityError) as exc:
            await session.execute(
                text(SHOT_SPEC_INSERT),
                shot_spec_params(
                    entity_id=f"spec_cross_{tenant.hex[:6]}",
                    project_id=project_a["project"],
                    # 修订属于另一个项目：同一租户、同一 schema，只有复合外键能拒。
                    shot_revision_id=project_b["revision"],
                    tenant=tenant,
                ),
            )
        assert _sqlstate(exc.value) == FK_VIOLATION, (
            f"跨项目绑定未被外键拒绝（SQLSTATE={_sqlstate(exc.value)}）"
        )
        await session.rollback()


@pytest.mark.parametrize(
    ("cut_points", "why"),
    [
        ([0, 10, 10], "非严格递增（重复点造出零长区间）"),
        ([0, 10, 5], "乱序"),
        ([5, 10], "首点不是 0"),
        ([], "空数组：CHECK 若不用 COALESCE 会求值为 NULL 而放行"),
    ],
)
async def test_ss3_is_enforced_by_the_database_check(
    admin_sessionmaker, cut_points: list[int], why: str
) -> None:
    """绕过领域对象的写入也必须被拒——这是 SS-3 定级 RUNTIME_ENFORCED 的根据。"""
    tenant = uuid.uuid4()
    ids = await _chain(admin_sessionmaker, tag=f"ss3_{tenant.hex[:6]}", tenant=tenant)

    async with admin_sessionmaker() as session:
        with pytest.raises(IntegrityError) as exc:
            await session.execute(
                text(SHOT_SPEC_INSERT),
                shot_spec_params(
                    entity_id=f"spec_ss3_{tenant.hex[:6]}",
                    project_id=ids["project"],
                    shot_revision_id=ids["revision"],
                    tenant=tenant,
                    cut_points=cut_points,
                ),
            )
        assert _sqlstate(exc.value) == CHECK_VIOLATION, f"{why} 未被库层 CHECK 拒绝"
        await session.rollback()


async def test_cut_point_equal_to_frame_count_is_rejected_by_the_check(
    admin_sessionmaker,
) -> None:
    """1000ms @25fps = 25 帧，合法帧号 0…24：末点 25 必须被拒。

    库层的整数除法与领域层 ``total_frames()`` 是同一算式，这条用例防止两处
    在未来某次修改中悄悄分叉。
    """
    tenant = uuid.uuid4()
    ids = await _chain(admin_sessionmaker, tag=f"edge_{tenant.hex[:6]}", tenant=tenant)

    async with admin_sessionmaker() as session:
        with pytest.raises(IntegrityError) as exc:
            await session.execute(
                text(SHOT_SPEC_INSERT),
                shot_spec_params(
                    entity_id=f"spec_edge_{tenant.hex[:6]}",
                    project_id=ids["project"],
                    shot_revision_id=ids["revision"],
                    tenant=tenant,
                    duration_ms=1000,
                    fps_num=25,
                    cut_points=[0, 24, 25],
                ),
            )
        assert _sqlstate(exc.value) == CHECK_VIOLATION
        await session.rollback()

    # 同一修订、末点 24 则合法：证明拒绝来自边界而不是别的约束。
    await seed_shot_spec(
        admin_sessionmaker,
        entity_id=f"spec_ok_{tenant.hex[:6]}",
        project_id=ids["project"],
        shot_revision_id=ids["revision"],
        tenant=tenant,
        duration_ms=1000,
        fps_num=25,
        cut_points=[0, 24],
    )


async def test_one_spec_per_revision_is_enforced_by_the_database(
    admin_sessionmaker,
) -> None:
    """同一修订第二份规格 23505：并发下「先查后插」必被穿透，唯一约束是唯一屏障。"""
    tenant = uuid.uuid4()
    ids = await _chain(admin_sessionmaker, tag=f"uniq_{tenant.hex[:6]}", tenant=tenant)

    await seed_shot_spec(
        admin_sessionmaker,
        entity_id=f"spec_first_{tenant.hex[:6]}",
        project_id=ids["project"],
        shot_revision_id=ids["revision"],
        tenant=tenant,
    )

    async with admin_sessionmaker() as session:
        with pytest.raises(IntegrityError) as exc:
            await session.execute(
                text(SHOT_SPEC_INSERT),
                shot_spec_params(
                    entity_id=f"spec_second_{tenant.hex[:6]}",
                    project_id=ids["project"],
                    shot_revision_id=ids["revision"],
                    tenant=tenant,
                ),
            )
        assert _sqlstate(exc.value) == UNIQUE_VIOLATION
        await session.rollback()
