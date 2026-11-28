"""ConstraintPack 的库层边界：RLS、租户复合外键、镜头归属触发器、部分唯一索引（A23）。

为什么这些判据必须在真实 PostgreSQL 上跑
------------------------------------------------------------
7.9 的不变量有两类实现方式：领域层构造校验与库层约束。领域层只能管住**经过
服务**的写入——运维脚本、后台补偿、将来的批处理直连数据库时，领域对象根本不在场。
「库层也拦得住」这句话不能由服务层再校验一遍来证明，只能由数据库自己回答。

七条判据各自对应一种失效形态，且互不覆盖：

1. **跨租户读为 0**：策略真的过滤行，而不是只写在 DDL 里；
2. **跨租户归属被拒 23503**：``(project_id, tenant_id)`` 复合外键真的生效——
   单列外键会放过「包属租户 A、项目属租户 B」；
3. **跨项目/悬空镜头被拒 23503**：``shot_ids`` 是数组，外键表达不了，靠触发器
   ``trg_constraint_packs_shots``；它抛的 SQLSTATE 必须与真外键一致，否则错误
   映射会多出一条「记得同步」的分支；
4. **同一逻辑包两个 ACTIVE 被拒 23505**：部分唯一索引，CP-6 的库层形态；
5. **不同逻辑包各自 ACTIVE 放行**：「至多一个」必须真的只约束同一 pack_id，
   若写成全表唯一，第二条失败就会伪装成「CP-6 生效」；
6. **同包同版本第二行被拒 23505**，同包不同版本放行：版本推进的库层形态；
7. **CHECK 负测（23514）**：重复镜头、空作用范围、未登记维度、条目缺字段、
   条目 id 重复、NOT_APPLICABLE 却列内容、版本非正、applicability 非法。

播种与负向写入一律走 admin 连接：用业务角色播种时，RLS 拒绝与约束拒绝会混成
同一种错误，负向用例就分不清是谁拒绝的。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from tests.conftest import set_test_scope
from tests.integration.planning_seed import (
    CONSTRAINT_ENTRY,
    CONSTRAINT_PACK_INSERT,
    constraint_pack_params,
    seed_episode,
    seed_project,
    seed_scene,
    seed_shot,
)

UNIQUE_VIOLATION = "23505"
FK_VIOLATION = "23503"
CHECK_VIOLATION = "23514"


def _sqlstate(exc: BaseException) -> str:
    orig = getattr(exc, "orig", exc)
    return getattr(orig, "sqlstate", "") or ""


async def _chain(admin_sessionmaker, *, tag: str, tenant, shots: int = 1) -> dict[str, object]:
    """建「项目 → 集数 → 场次 → 镜头*n」，返回各级 id 与镜头 id 列表。"""
    project = f"proj_{tag}"
    episode = f"ep_{tag}"
    scene = f"scene_{tag}"
    await seed_project(admin_sessionmaker, project_id=project, tenant=tenant)
    await seed_episode(admin_sessionmaker, episode_id=episode, project_id=project, tenant=tenant)
    await seed_scene(
        admin_sessionmaker,
        scene_id=scene,
        project_id=project,
        episode_id=episode,
        tenant=tenant,
        code=f"SC-{tag}",
    )
    shot_ids = [f"shot_{tag}_{i}" for i in range(shots)]
    for index, shot_id in enumerate(shot_ids):
        await seed_shot(
            admin_sessionmaker,
            shot_id=shot_id,
            scene_id=scene,
            project_id=project,
            tenant=tenant,
            code=f"SH-{tag}-{index}",
        )
    return {"project": project, "scene": scene, "shots": shot_ids}


async def test_constraint_packs_are_invisible_across_tenants(
    db_sessionmaker, admin_sessionmaker
) -> None:
    """跨租户读为 0、本租户读为 1（组织维度经父项目存在性传递）。"""
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    tag = f"cp_rls_{tenant_a.hex[:6]}"
    chain = await _chain(admin_sessionmaker, tag=tag, tenant=tenant_a)
    pack_id = f"pack_{tag}"

    async with admin_sessionmaker() as session:
        await session.execute(
            text(CONSTRAINT_PACK_INSERT),
            constraint_pack_params(
                entity_id=pack_id,
                project_id=chain["project"],
                tenant=tenant_a,
                shot_ids=list(chain["shots"]),
                scene_id=chain["scene"],
            ),
        )
        await session.commit()

    async with db_sessionmaker() as session:
        await set_test_scope(session, tenant_id=tenant_a)
        own = await session.scalar(
            text("SELECT count(*) FROM project_planning.constraint_packs WHERE entity_id = :eid"),
            {"eid": pack_id},
        )
        assert own == 1

        await set_test_scope(session, tenant_id=tenant_b)
        other = await session.scalar(
            text("SELECT count(*) FROM project_planning.constraint_packs WHERE entity_id = :eid"),
            {"eid": pack_id},
        )
        assert other == 0, "跨租户读到了约束包：策略没过滤行（或未 ENABLE/FORCE）"
        await session.rollback()


async def test_pack_and_project_must_share_a_tenant(admin_sessionmaker) -> None:
    """``(project_id, tenant_id)`` 复合外键：单列外键会放过「包属 A、项目属 B」。"""
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    chain_b = await _chain(admin_sessionmaker, tag=f"cp_fk_{tenant_b.hex[:6]}", tenant=tenant_b)

    async with admin_sessionmaker() as session:
        with pytest.raises(IntegrityError) as exc:
            await session.execute(
                text(CONSTRAINT_PACK_INSERT),
                constraint_pack_params(
                    entity_id=f"pack_x_{tenant_a.hex[:6]}",
                    # 项目属于另一个租户：同一 schema，只有复合外键能拒。
                    project_id=chain_b["project"],
                    tenant=tenant_a,
                    shot_ids=list(chain_b["shots"]),
                ),
            )
        assert _sqlstate(exc.value) == FK_VIOLATION, (
            f"跨租户归属未被外键拒绝（SQLSTATE={_sqlstate(exc.value)}）"
        )
        await session.rollback()


@pytest.mark.parametrize("foreign", [False, True], ids=["cross_project", "dangling"])
async def test_shots_must_exist_in_the_same_project(admin_sessionmaker, foreign: bool) -> None:
    """``shot_ids`` 的每个元素必须是本项目的真实镜头（数组无法外键，靠触发器）。

    ``foreign=True``：真实存在但属于另一个项目（服务层最难自查的那一种）；
    ``foreign=False``：根本不存在的 id。两者的 SQLSTATE 必须相同——都是「引用
    不成立」，共用同一条错误映射，不为触发器新增翻译分支。
    """
    tenant = uuid.uuid4()
    tag = f"cp_trg_{tenant.hex[:6]}"
    chain = await _chain(admin_sessionmaker, tag=tag, tenant=tenant)
    other = await _chain(admin_sessionmaker, tag=f"{tag}_other", tenant=tenant, shots=1)
    intruder = other["shots"][0] if foreign else f"shot_missing_{tag}"

    async with admin_sessionmaker() as session:
        with pytest.raises(IntegrityError) as exc:
            await session.execute(
                text(CONSTRAINT_PACK_INSERT),
                constraint_pack_params(
                    entity_id=f"pack_trg_{tag}",
                    project_id=chain["project"],
                    tenant=tenant,
                    shot_ids=[*list(chain["shots"]), intruder],
                ),
            )
        assert _sqlstate(exc.value) == FK_VIOLATION, (
            f"越界镜头未被拒绝（SQLSTATE={_sqlstate(exc.value)}）"
        )
        await session.rollback()


async def test_two_active_versions_of_one_pack_are_rejected(admin_sessionmaker) -> None:
    """CP-6 的库层形态：同一逻辑包的第二个 ACTIVE 必须 23505。

    两个 ACTIVE 并存会让「哪一份生效」没有答案，而下游必须按 pack 查锁定维度。
    """
    tenant = uuid.uuid4()
    tag = f"cp_act_{tenant.hex[:6]}"
    chain = await _chain(admin_sessionmaker, tag=tag, tenant=tenant)
    pack_id = f"cp_logical_{tag}"

    await _insert_pack(
        admin_sessionmaker,
        entity_id=f"pack_v1_{tag}",
        pack_id=pack_id,
        chain=chain,
        tenant=tenant,
        version=1,
        status="ACTIVE",
    )

    async with admin_sessionmaker() as session:
        with pytest.raises(IntegrityError) as exc:
            await session.execute(
                text(CONSTRAINT_PACK_INSERT),
                constraint_pack_params(
                    entity_id=f"pack_v2_{tag}",
                    constraint_pack_id=pack_id,
                    project_id=chain["project"],
                    tenant=tenant,
                    shot_ids=list(chain["shots"]),
                    version=2,
                    status="ACTIVE",
                ),
            )
        assert _sqlstate(exc.value) == UNIQUE_VIOLATION, (
            f"第二个 ACTIVE 未被部分唯一索引拒绝（SQLSTATE={_sqlstate(exc.value)}）"
        )
        await session.rollback()


async def test_different_packs_may_each_have_an_active_version(
    admin_sessionmaker,
) -> None:
    """「至多一个 ACTIVE」只约束同一 pack_id。

    这条用例防的是一种假绿：把部分唯一索引写成全表唯一，上面那条负测照样过，
    而系统里从此只能存在一个生效的约束包。
    """
    tenant = uuid.uuid4()
    tag = f"cp_two_{tenant.hex[:6]}"
    chain = await _chain(admin_sessionmaker, tag=tag, tenant=tenant)

    await _insert_pack(
        admin_sessionmaker,
        entity_id=f"pack_a_{tag}",
        pack_id=f"cp_a_{tag}",
        chain=chain,
        tenant=tenant,
        status="ACTIVE",
    )
    await _insert_pack(
        admin_sessionmaker,
        entity_id=f"pack_b_{tag}",
        pack_id=f"cp_b_{tag}",
        chain=chain,
        tenant=tenant,
        status="ACTIVE",
    )

    async with admin_sessionmaker() as session:
        active = await session.scalar(
            text(
                "SELECT count(*) FROM project_planning.constraint_packs"
                " WHERE project_id = :pid AND status = 'ACTIVE'"
            ),
            {"pid": chain["project"]},
        )
    assert active == 2


async def test_same_pack_version_cannot_exist_twice(admin_sessionmaker) -> None:
    """``uq_constraint_pack_version``：同包同版本第二行必须 23505，换版本则放行。

    这条约束是「不得原地覆盖」的库层表达：内容变了就得换行，而换了行就必然
    换了 version，否则两份不同内容会自称同一版本，让「哪一份是 v1」没有答案。
    """
    tenant = uuid.uuid4()
    tag = f"cp_ver_{tenant.hex[:6]}"
    chain = await _chain(admin_sessionmaker, tag=tag, tenant=tenant)
    pack_id = f"cp_ver_{tag}"

    await _insert_pack(
        admin_sessionmaker,
        entity_id=f"pack_v1_{tag}",
        pack_id=pack_id,
        chain=chain,
        tenant=tenant,
        version=1,
    )

    async with admin_sessionmaker() as session:
        with pytest.raises(IntegrityError) as exc:
            await session.execute(
                text(CONSTRAINT_PACK_INSERT),
                constraint_pack_params(
                    entity_id=f"pack_dup_{tag}",
                    constraint_pack_id=pack_id,
                    project_id=chain["project"],
                    tenant=tenant,
                    shot_ids=list(chain["shots"]),
                    version=1,
                ),
            )
        assert _sqlstate(exc.value) == UNIQUE_VIOLATION
        await session.rollback()

    # 换版本（v2，仍是 DRAFT）：同一逻辑包允许并存多个未生效版本。
    await _insert_pack(
        admin_sessionmaker,
        entity_id=f"pack_v2_{tag}",
        pack_id=pack_id,
        chain=chain,
        tenant=tenant,
        version=2,
    )


def _duplicate(shots: list[str]) -> list[str]:
    return [shots[0], shots[0]]


def _empty(shots: list[str]) -> list[str]:
    return []


@pytest.mark.parametrize(
    ("shots_of", "overrides", "why"),
    [
        (_duplicate, {}, "作用镜头重复（同一镜头两次）"),
        (_empty, {}, "作用范围为空（包没有约束对象）"),
        (None, {"shot_group_ids": ["g1", "g1"]}, "镜头组重复（同一函数、另一列）"),
        (None, {"shot_group_ids": ["g1", "   "]}, "镜头组含空白元素"),
        (None, {"locked_dimensions": ["cut_points", "color_grading"]}, "未登记的锁定维度"),
        (None, {"constraints": [{"constraint_id": "c-1"}]}, "条目缺七项必填字段"),
        (
            None,
            {"constraints": [{**CONSTRAINT_ENTRY, "approval_ref": "  "}]},
            "条目字段是空白串（非「非空」）",
        ),
        (
            None,
            {"constraints": [CONSTRAINT_ENTRY, dict(CONSTRAINT_ENTRY)]},
            "constraint_id 包内重复",
        ),
        (
            None,
            {"applicability": "NOT_APPLICABLE", "locked_dimensions": ["cut_points"]},
            "NOT_APPLICABLE 却列了锁定维度",
        ),
        (None, {"version": 0}, "版本非正"),
        (None, {"applicability": "MAYBE"}, "适用性取值不在枚举内"),
    ],
    ids=[
        "duplicate_shots",
        "empty_shots",
        "duplicate_groups",
        "blank_group",
        "unknown_dimension",
        "entry_missing_fields",
        "entry_blank_field",
        "entry_duplicate_id",
        "not_applicable_with_content",
        "non_positive_version",
        "bad_applicability",
    ],
)
async def test_pack_checks_are_enforced_by_the_database(
    admin_sessionmaker, shots_of, overrides: dict[str, object], why: str
) -> None:
    """绕过领域对象的写入也必须被拒——这是 CP-1/2/3/4 定级 RUNTIME_ENFORCED 的根据。

    ``shot_ids`` 的**空白元素**不在本用例里：触发器先于 CHECK 执行，而空白 id 必然
    找不到对应镜头，于是拒绝理由会是 23503（引用不成立）而不是 23514。断言一个
    取决于触发器与 CHECK 执行顺序的 SQLSTATE 是脆弱的；空白的判定由领域层单测
    覆盖，库层的空白分支改用 ``shot_group_ids``（无触发器，直达 CHECK）。
    """
    tenant = uuid.uuid4()
    tag = f"cp_ck_{tenant.hex[:6]}"
    chain = await _chain(admin_sessionmaker, tag=tag, tenant=tenant)
    existing = [str(s) for s in chain["shots"]]
    params = constraint_pack_params(
        entity_id=f"pack_ck_{tag}",
        project_id=chain["project"],
        tenant=tenant,
        shot_ids=shots_of(existing) if shots_of is not None else existing,
        **overrides,
    )

    async with admin_sessionmaker() as session:
        with pytest.raises(IntegrityError) as exc:
            await session.execute(text(CONSTRAINT_PACK_INSERT), params)
        assert _sqlstate(exc.value) == CHECK_VIOLATION, f"{why} 未被库层 CHECK 拒绝"
        await session.rollback()


async def test_not_applicable_pack_with_nothing_listed_is_accepted(
    admin_sessionmaker,
) -> None:
    """NOT_APPLICABLE + 空维度 + 空条目是合法形态，证明上一条不是「一律拒绝」。"""
    tenant = uuid.uuid4()
    tag = f"cp_na_{tenant.hex[:6]}"
    chain = await _chain(admin_sessionmaker, tag=tag, tenant=tenant)

    async with admin_sessionmaker() as session:
        await session.execute(
            text(CONSTRAINT_PACK_INSERT),
            constraint_pack_params(
                entity_id=f"pack_na_{tag}",
                project_id=chain["project"],
                tenant=tenant,
                shot_ids=list(chain["shots"]),
                applicability="NOT_APPLICABLE",
                locked_dimensions=[],
                constraints=[],
                status="ACTIVE",
                approved_by="director-1",
                approved_at=datetime(2026, 11, 21, tzinfo=UTC),
            ),
        )
        await session.commit()


async def _insert_pack(
    admin_sessionmaker,
    *,
    entity_id: str,
    pack_id: str,
    chain: dict[str, object],
    tenant,
    version: int = 1,
    status: str = "DRAFT",
) -> None:
    async with admin_sessionmaker() as session:
        await session.execute(
            text(CONSTRAINT_PACK_INSERT),
            constraint_pack_params(
                entity_id=entity_id,
                constraint_pack_id=pack_id,
                project_id=chain["project"],
                tenant=tenant,
                shot_ids=list(chain["shots"]),
                scene_id=chain["scene"],
                version=version,
                status=status,
            ),
        )
        await session.commit()
