"""归属外键约束负向测试（F04 + 审查 P1-03：数据库层锁死归属真实性）。

验证数据库级归属约束真实生效（R2 不变量放最可靠层）：

1. SCOPE-1：projects 以**租户复合外键** (organization_id, tenant_id) 引用
   organizations —— 引用不存在组织必须被拒绝；引用**存在但属另一租户**的
   组织同样必须被拒绝（单列外键只能证明组织存在，不能证明归属正确）。
2. SCOPE-2：episodes.project_id 外键 -> projects(entity_id)
   —— 写入引用不存在项目的集数必须被数据库拒绝，而非仅靠 Service 校验。
3. SCOPE-3：asset_versions.project_id 复合外键 (project_id, tenant_id) ->
   projects(entity_id, tenant_id) —— 资产必须挂到真实存在且**同租户**的项目上；
   跨租户挂靠在数据库层被拒（第二轮审查 P1-04）。
4. organizations 自身纳入租户 RLS —— 归属根不得跨租户可读。
5. 归属外键的**覆盖率**（SCOPE-5）：每一张含 ``project_id`` 的表都必须有
   「覆盖 ``tenant_id`` 且已 ``VALIDATE``」的 projects 外键，集合从
   ``information_schema`` **派生**，不抄清单。前面四条是逐点负向测试，
   它们的集合是人写的——13 张表集体漏约束时，一条都不会红（D2 出证即如此：
   台账 FAIL 2/15，而这四条全绿）。所以「集合」本身必须成为判据。
6. 归属数据的**可复算违例行数**（SCOPE-6）：孤儿行与租户不一致行必须为 0，
   且必须用**超级用户**连接统计——RLS 会把越权行静默过滤，「0 条违规」可能
   只是「我看不到的那些里没有违规」。

**为什么有些用例要跑两遍（ADR-R24 之后新增）**
------------------------------------------------
组织维度 RLS 生效后，子表策略用 ``EXISTS (父表)`` 表达归属（迁移
``e9f0a1b2c3d4``）。于是「父实体不存在」与「父实体不在当前作用域」在数据库里
是**同一个谓词**，写入会先被 ``WITH CHECK`` 拒绝（SQLSTATE 42501），外键的
AFTER 触发器永远轮不到。

只用业务连接做负向测试，就再也证明不了「外键真的存在且独立生效」——
而这正是 SCOPE-2/SCOPE-3 的内容。所以这几条用例分成两层跑：

| 连接 | RLS | 期望的拒绝者 | SQLSTATE |
| --- | --- | --- | --- |
| 业务角色（project/asset 域角色） | 生效 | 行级安全（父级不在作用域） | 42501 |
| 管理员（超级用户，``admin_sessionmaker``） | 不生效 | 外键 | 23503 |

两层各自拒绝，才叫两层防线；只有一层时，另一层的「证据」就只是注释。

依赖真实 PostgreSQL，无 PG 环境时本文件全部跳过（CI 有 PG 时执行）。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, ProgrammingError

from packages.shared.storage_locator import build_locator
from tests.conftest import set_test_scope

# 期望的 SQLSTATE：外键冲突（外键层）与行级安全拒绝（策略层）。
FK_VIOLATION = "23503"
RLS_DENIAL = "42501"


def _now() -> datetime:
    return datetime.now(UTC)


def _sqlstate(exc: BaseException) -> str:
    """取驱动 SQLSTATE（SQLAlchemy 把原始 asyncpg 异常放在 ``orig`` 上）。"""
    orig = getattr(exc, "orig", exc)
    return getattr(orig, "sqlstate", "") or ""


async def test_project_must_reference_existing_organization(db_sessionmaker) -> None:
    """写引用不存在组织的项目 → 复合外键拒绝（SCOPE-1）。

    这里不需要两层：项目的组织列**就是**它自己的归属键，把会话组织上下文设成
    该组织即可让 RLS 的 ``WITH CHECK`` 通过，于是拒绝者只能是外键。也就是说，
    这一条天然证明了「外键而不是策略在把关归属真实性」。
    """
    sm = db_sessionmaker
    nonexistent_org = uuid.uuid4()
    tenant = uuid.uuid4()

    async with sm() as session:
        await set_test_scope(session, nonexistent_org, tenant_id=tenant)
        with pytest.raises(IntegrityError) as exc_info:
            await session.execute(
                text(
                    """
                    INSERT INTO project_planning.projects
                        (entity_id, entity_type, schema_version, entity_version,
                         status, owner_module, tenant_id, created_at, created_by,
                         updated_at, project_name, project_tier, organization_id)
                    VALUES
                        (:eid, 'Project', 205, 1, 'DRAFT', 'project_planning',
                         :tid, :now, 'tester', :now, :name, 'B', :org)
                    """
                ),
                {
                    "eid": uuid.uuid4().hex,
                    "tid": tenant,
                    "now": _now(),
                    "name": "归属缺失项目",
                    "org": nonexistent_org,
                },
            )
        assert _sqlstate(exc_info.value) == FK_VIOLATION
        await session.rollback()


async def test_episode_must_reference_existing_project(
    db_sessionmaker, admin_sessionmaker
) -> None:
    """写引用不存在项目的集数 → 数据库层拒绝（SCOPE-2）。

    两层证据，缺一不可：

    - 业务连接：父项目在当前作用域不可见 → RLS ``WITH CHECK`` 拒绝（42501）；
    - 管理员连接：RLS 不生效 → 外键拒绝（23503）。

    若只留前者，「外键是否存在」就没有任何运行时证据；若只留后者，就测不到
    生产连接上的真实失败形态。
    """
    tenant = uuid.uuid4()
    dangling_project = uuid.uuid4().hex
    insert = text(
        """
        INSERT INTO project_planning.episodes
            (entity_id, entity_type, schema_version, entity_version,
             status, owner_module, tenant_id, created_at, created_by,
             updated_at, project_id, episode_ref, title)
        VALUES
            (:eid, 'Episode', 205, 1, 'DRAFT', 'project_planning',
             :tid, :now, 'tester', :now, :pid, 'EP01', '悬空集数')
        """
    )
    params = {
        "eid": uuid.uuid4().hex,
        "tid": tenant,
        "now": _now(),
        "pid": dangling_project,
    }

    # 层 1：业务角色（RLS 生效）——策略先拒。
    async with db_sessionmaker() as session:
        await set_test_scope(session, tenant_id=tenant)
        with pytest.raises(ProgrammingError) as exc_info:
            await session.execute(insert, params)
        assert _sqlstate(exc_info.value) == RLS_DENIAL
        await session.rollback()

    # 层 2：管理员连接（RLS 不生效）——外键必须独立拒绝。
    async with admin_sessionmaker() as session:
        with pytest.raises(IntegrityError) as exc_info:
            await session.execute(insert, params)
        assert _sqlstate(exc_info.value) == FK_VIOLATION
        await session.rollback()


_ORGANIZATION_INSERT = """
    INSERT INTO project_planning.organizations
        (organization_id, tenant_id, name, created_at, created_by, updated_at)
    VALUES (:org, :tid, :name, :now, 'tester', :now)
"""


async def _insert_organization(admin_sessionmaker, *, org_id, tenant, name="测试组织") -> None:
    """用**管理连接**播种组织行（DATA-6 之后的唯一可行方式）。

    业务域角色对 ``organizations`` 只有 SELECT：契约里没有任何 ``ORGANIZATION.*``
    动作，组织是平台级归属根，由管理面/运维创建。播种走管理连接不是「绕过被测
    边界」，而是如实对应「管理面建组织」这条尚未实现的路径——若改用放宽授权
    让播种成功，就是用测试需求反过来削弱生产的最小权限（方向反了）。
    """
    async with admin_sessionmaker() as session:
        await session.execute(
            text(_ORGANIZATION_INSERT),
            {"org": org_id, "tid": tenant, "now": _now(), "name": name},
        )
        await session.commit()


async def _drop_organization(admin_sessionmaker, org_id) -> None:
    """清理播种的组织行（管理连接；业务角色既无 DELETE 也无须有）。"""
    async with admin_sessionmaker() as session:
        await session.execute(
            text(
                "DELETE FROM project_planning.organizations "
                "WHERE organization_id = :org"
            ),
            {"org": org_id},
        )
        await session.commit()


async def test_project_with_valid_organization_succeeds(
    db_sessionmaker, admin_sessionmaker
) -> None:
    """正向：先建组织再建归属项目 → 外键约束放行（SCOPE-1 不误伤合法路径）。"""
    sm = db_sessionmaker
    tenant = uuid.uuid4()
    org_id = uuid.uuid4()

    await _insert_organization(admin_sessionmaker, org_id=org_id, tenant=tenant)
    try:
        async with sm() as session:
            await set_test_scope(session, org_id, tenant_id=tenant)
            # 归属该组织的项目（不抛异常即通过）。
            await session.execute(
                text(
                    """
                    INSERT INTO project_planning.projects
                        (entity_id, entity_type, schema_version, entity_version,
                         status, owner_module, tenant_id, created_at, created_by,
                         updated_at, project_name, project_tier, organization_id)
                    VALUES
                        (:eid, 'Project', 205, 1, 'DRAFT', 'project_planning',
                         :tid, :now, 'tester', :now, :name, 'B', :org)
                    """
                ),
                {
                    "eid": uuid.uuid4().hex,
                    "tid": tenant,
                    "now": _now(),
                    "name": "合法归属项目",
                    "org": org_id,
                },
            )
            await session.rollback()
    finally:
        await _drop_organization(admin_sessionmaker, org_id)


async def test_project_cannot_reference_cross_tenant_organization(
    db_sessionmaker, admin_sessionmaker
) -> None:
    """项目引用**另一租户**的组织 → 复合外键拒绝（审查 P1-03）。

    这是单列外键漏掉的那一类：组织真实存在，但属于租户 A，而项目写进
    租户 B。只有把 tenant_id 纳入外键，数据库才能拒绝这种归属错配。

    组织上下文按「项目声明的组织」注入，让 RLS 的 ``WITH CHECK`` 通过；
    于是拒绝者只能是跨租户复合外键——若哪天这里变成 42501，说明策略把
    跨租户场景也接管了，外键证据随之消失，必须显式重审。
    """
    sm = db_sessionmaker
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    org_in_a = uuid.uuid4()

    # 1. 在租户 A 建组织（管理连接，见 _insert_organization 的说明）。
    await _insert_organization(
        admin_sessionmaker, org_id=org_in_a, tenant=tenant_a, name="A 租户组织"
    )
    try:
        async with sm() as session:
            # 2. 切到租户 B，写入引用 A 组织的项目 → 必须被复合外键拒绝。
            await set_test_scope(session, org_in_a, tenant_id=tenant_b)
            with pytest.raises(IntegrityError) as exc_info:
                await session.execute(
                    text(
                        """
                        INSERT INTO project_planning.projects
                            (entity_id, entity_type, schema_version, entity_version,
                             status, owner_module, tenant_id, created_at, created_by,
                             updated_at, project_name, project_tier, organization_id)
                        VALUES
                            (:eid, 'Project', 205, 1, 'DRAFT', 'project_planning',
                             :tid, :now, 'tester', :now, :name, 'B', :org)
                        """
                    ),
                    {
                        "eid": uuid.uuid4().hex,
                        "tid": tenant_b,
                        "now": _now(),
                        "name": "跨租户归属项目",
                        "org": org_in_a,
                    },
                )
            assert _sqlstate(exc_info.value) == FK_VIOLATION
            await session.rollback()
    finally:
        await _drop_organization(admin_sessionmaker, org_in_a)


_ASSET_INSERT = """
    INSERT INTO asset_service.asset_versions
        (entity_id, entity_type, schema_version, entity_version, status,
         owner_module, tenant_id, created_at, created_by, updated_at,
         project_id, asset_type, sha256, storage_ref, source_asset_version_ids)
    VALUES
        (:eid, 'AssetVersion', 205, 1, 'GENERATED', 'asset-service', :tid,
         :now, 'tester', :now, :pid, 'IMAGE', :sha, :locator, '[]'::jsonb)
"""


async def _insert_project(db_sessionmaker, *, project_id: str, tenant) -> None:
    """用项目域角色写入并提交一个真实项目（资产归属的引用目标）。"""
    async with db_sessionmaker() as session:
        await set_test_scope(session, tenant_id=tenant)
        await session.execute(
            text(
                """
                INSERT INTO project_planning.projects
                    (entity_id, entity_type, schema_version, entity_version,
                     status, owner_module, tenant_id, created_at, created_by,
                     updated_at, project_name, project_tier, organization_id)
                VALUES
                    (:eid, 'Project', 205, 1, 'DRAFT', 'project_planning',
                     :tid, :now, 'tester', :now, :name, 'B', NULL)
                """
            ),
            {"eid": project_id, "tid": tenant, "now": _now(), "name": "归属测试项目"},
        )
        await session.commit()


async def test_asset_must_reference_existing_project(
    asset_sessionmaker, admin_sessionmaker
) -> None:
    """资产引用不存在项目 → 数据库层拒绝（SCOPE-3 / 第二轮审查 P1-04）。

    两层证据同 ``test_episode_must_reference_existing_project``：业务连接被
    RLS 拒绝（42501），管理员连接被外键拒绝（23503）。

    这是 P1-04 的可执行证明：整改前模型里根本没有 project_id，资产无法表达
    归属；现在「挂着不存在的项目」在数据库层就写不进去。
    """
    tenant = uuid.uuid4()
    params = {
        "eid": uuid.uuid4().hex,
        "tid": tenant,
        "now": _now(),
        "pid": uuid.uuid4().hex,
        "sha": "a" * 64,
        "locator": build_locator("a" * 64, "png"),
    }

    async with asset_sessionmaker() as session:
        await set_test_scope(session, tenant_id=tenant)
        with pytest.raises(ProgrammingError) as exc_info:
            await session.execute(text(_ASSET_INSERT), params)
        assert _sqlstate(exc_info.value) == RLS_DENIAL
        await session.rollback()

    async with admin_sessionmaker() as session:
        with pytest.raises(IntegrityError) as exc_info:
            await session.execute(text(_ASSET_INSERT), params)
        assert _sqlstate(exc_info.value) == FK_VIOLATION
        await session.rollback()


async def test_asset_cannot_reference_cross_tenant_project(
    db_sessionmaker, asset_sessionmaker, admin_sessionmaker
) -> None:
    """资产引用**另一租户**的项目 → 复合外键拒绝（单列外键漏掉的那一类）。"""
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    project_in_a = uuid.uuid4().hex
    await _insert_project(db_sessionmaker, project_id=project_in_a, tenant=tenant_a)
    params = {
        "eid": uuid.uuid4().hex,
        "tid": tenant_b,
        "now": _now(),
        "pid": project_in_a,
        "sha": "b" * 64,
        "locator": build_locator("b" * 64, "png"),
    }

    # 层 1：业务角色切到租户 B 写资产，指向租户 A 的项目 → RLS 先拒。
    async with asset_sessionmaker() as session:
        await set_test_scope(session, tenant_id=tenant_b)
        with pytest.raises(ProgrammingError) as exc_info:
            await session.execute(text(_ASSET_INSERT), params)
        assert _sqlstate(exc_info.value) == RLS_DENIAL
        await session.rollback()

    # 层 2：管理员连接 → 复合外键（project_id, tenant_id）拒绝跨租户挂靠。
    async with admin_sessionmaker() as session:
        with pytest.raises(IntegrityError) as exc_info:
            await session.execute(text(_ASSET_INSERT), params)
        assert _sqlstate(exc_info.value) == FK_VIOLATION
        await session.rollback()


async def test_asset_with_same_tenant_project_succeeds(
    db_sessionmaker, asset_sessionmaker
) -> None:
    """正向：同租户真实项目下登记资产 → 外键放行（不误伤合法路径）。"""
    tenant = uuid.uuid4()
    project_id = uuid.uuid4().hex
    await _insert_project(db_sessionmaker, project_id=project_id, tenant=tenant)

    async with asset_sessionmaker() as session:
        await set_test_scope(session, tenant_id=tenant)
        await session.execute(
            text(_ASSET_INSERT),
            {
                "eid": uuid.uuid4().hex,
                "tid": tenant,
                "now": _now(),
                "pid": project_id,
                "sha": "c" * 64,
                "locator": build_locator("c" * 64, "png"),
            },
        )
        await session.rollback()


async def test_organizations_are_tenant_isolated(
    db_sessionmaker, admin_sessionmaker
) -> None:
    """organizations 表纳入租户 RLS：别的租户看不到本租户的组织（审查 R8）。

    组织上下文**两次都注入同一个组织**，让组织维度恒定成立——这样"不可见"
    只可能来自租户维度。若这里把组织上下文清空，用例会因为组织谓词而通过，
    测到的就不是租户隔离（D3 之后这是最容易写出假阳性的地方）。

    播种走管理连接（业务角色对 organizations 只有 SELECT，DATA-6）；**读取仍走
    业务角色**——被测的是 RLS 的可见性，不是播种路径。
    """
    sm = db_sessionmaker
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    org_in_a = uuid.uuid4()

    await _insert_organization(
        admin_sessionmaker, org_id=org_in_a, tenant=tenant_a, name="A 租户组织"
    )
    try:
        async with sm() as session:
            # A 上下文可见。
            await set_test_scope(session, org_in_a, tenant_id=tenant_a)
            visible_a = await session.scalar(
                text(
                    "SELECT count(*) FROM project_planning.organizations "
                    "WHERE organization_id = :org"
                ),
                {"org": org_in_a},
            )
            assert visible_a == 1

            # B 上下文不可见（租户维度过滤；组织维度此处仍然匹配）。
            await set_test_scope(session, org_in_a, tenant_id=tenant_b)
            visible_b = await session.scalar(
                text(
                    "SELECT count(*) FROM project_planning.organizations "
                    "WHERE organization_id = :org"
                ),
                {"org": org_in_a},
            )
            assert visible_b == 0
            await session.rollback()
    finally:
        await _drop_organization(admin_sessionmaker, org_in_a)


# ---------------------------------------------------------------------------
# SCOPE-5 / SCOPE-6：集合派生式归属闸门
#
# 前面四条用例验的是「某条外键真的会拒绝越权写入」，但它们的**集合是人写的**。
# 于是 D2 出证时出现了一个悖论：台账判定 FAIL（2/15），而本文件全绿——因为漏掉
# 的那 13 张表从来没有被任何用例提到过。
#
# 「清单不会自己举手」：漏掉的条目不报错，它只是不在那里。所以下面两条用例
# 的目标集合一律从 catalog 派生——新增一张带 project_id 的表，若忘了挂约束，
# 用例立刻红，不需要有人记得来加一行。
# ---------------------------------------------------------------------------


async def _derived_project_scoped_tables(session) -> list[tuple[str, str, bool]]:
    """派生「含 project_id 的基表」及「该表是否含 tenant_id」。

    只取 ``BASE TABLE``：``information_schema.columns`` 同时覆盖视图，把视图
    算进来会要求给视图挂外键——一个既做不到也不该做的事，最后只能加豁免，
    而豁免一旦加上就再没人回头看它是否仍然必要。
    """
    rows = (
        await session.execute(
            text(
                """
                SELECT c.table_schema, c.table_name,
                       EXISTS (
                           SELECT 1 FROM information_schema.columns t
                           WHERE t.table_schema = c.table_schema
                             AND t.table_name = c.table_name
                             AND t.column_name = 'tenant_id'
                       ) AS has_tenant
                FROM information_schema.columns c
                JOIN information_schema.tables tb
                  ON tb.table_schema = c.table_schema
                 AND tb.table_name = c.table_name
                WHERE c.column_name = 'project_id'
                  AND tb.table_type = 'BASE TABLE'
                  AND c.table_schema NOT IN ('pg_catalog', 'information_schema')
                ORDER BY 1, 2
                """
            )
        )
    ).all()
    return [
        (str(r.table_schema), str(r.table_name), bool(r.has_tenant)) for r in rows
    ]


async def _projects_table(session) -> str:
    """定位归属根表，多一张同名表就报错而不是挑一个。"""
    rows = (
        await session.execute(
            text(
                """
                SELECT n.nspname
                FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE c.relname = 'projects' AND c.relkind IN ('r', 'p')
                  AND n.nspname NOT IN ('pg_catalog', 'information_schema')
                """
            )
        )
    ).all()
    schemas = [str(r.nspname) for r in rows]
    assert len(schemas) == 1, (
        f"归属根 projects 应恰好存在于一个 schema，实际 {schemas}；"
        "多个同名表意味着归属模型分叉，必须先裁定再谈约束"
    )
    return f"{schemas[0]}.projects"


async def _project_fk_edges(session, projects: str) -> dict[str, list[tuple[str, bool, list[str]]]]:
    """取所有「子表 -> projects」的外键：子表名 -> [(约束名, 是否已验证, 列集)]。"""
    rows = (
        await session.execute(
            text(
                """
                SELECT cn.nspname || '.' || cc.relname AS child,
                       con.conname,
                       con.convalidated,
                       COALESCE(
                           (SELECT array_agg(a.attname::text ORDER BY k.ord)
                            FROM unnest(con.conkey) WITH ORDINALITY AS k(attnum, ord)
                            JOIN pg_attribute a
                              ON a.attrelid = con.conrelid AND a.attnum = k.attnum),
                           ARRAY[]::text[]
                       ) AS cols
                FROM pg_constraint con
                JOIN pg_class cc ON cc.oid = con.conrelid
                JOIN pg_namespace cn ON cn.oid = cc.relnamespace
                JOIN pg_class pc ON pc.oid = con.confrelid
                JOIN pg_namespace pn ON pn.oid = pc.relnamespace
                WHERE con.contype = 'f'
                  AND pn.nspname || '.' || pc.relname = :projects
                """
            ),
            {"projects": projects},
        )
    ).all()
    edges: dict[str, list[tuple[str, bool, list[str]]]] = {}
    for row in rows:
        edges.setdefault(str(row.child), []).append(
            (str(row.conname), bool(row.convalidated), [str(c) for c in row.cols])
        )
    return edges


async def test_every_project_scoped_table_has_tenant_covering_fk(
    admin_sessionmaker,
) -> None:
    """每张含 project_id 的表都必须有覆盖 tenant_id 且已 VALIDATE 的 projects 外键。

    判据（三件事同时成立，任一条单独成立都不够）：

    1. **有**一条指向 projects 的外键，且其列集覆盖 ``{project_id, tenant_id}``
       （无 tenant_id 的表退化为 ``{project_id}``）；
    2. 该外键 ``convalidated = true``。``NOT VALID`` 的外键只对**新写入**生效，
       存量行从未被检查过——台账要求的是「存在 ∧ convalidated ∧ 违规行数 0」，
       只建不验等于「拒绝永久豁免」那条红线被绕过；
    3. **没有**冗余的单列 ``project_id -> projects`` 外键。复合外键蕴含单列，
       并存就是两份事实源，且每次写入多一次索引查找。这一条同时防止「修 CI 的
       人把复合外键删掉、退回单列」。

    为什么这条不能用「逐表硬编码」实现：D2 的缺口正是「15 张表里 13 张漏了」，
    而写死的清单在新增第 16 张表时只是漏掉——不报错。
    """
    async with admin_sessionmaker() as session:
        projects = await _projects_table(session)
        targets = await _derived_project_scoped_tables(session)
        assert targets, (
            "派生不到任何含 project_id 的基表：库可能未迁移到 head，"
            "或归属模型被改得面目全非——两种情况都必须先查清再放行"
        )
        edges = await _project_fk_edges(session, projects)

    missing: list[str] = []
    unvalidated: list[str] = []
    redundant: list[str] = []

    for schema, table, has_tenant in targets:
        child = f"{schema}.{table}"
        required = {"project_id", "tenant_id"} if has_tenant else {"project_id"}
        child_edges = edges.get(child, [])
        covering = [edge for edge in child_edges if required <= set(edge[2])]
        if not covering:
            missing.append(f"{child}（需要覆盖 {sorted(required)}）")
            continue
        if not any(edge[1] for edge in covering):
            unvalidated.append(
                f"{child}（{', '.join(e[0] for e in covering)} 尚未 VALIDATE）"
            )
        if has_tenant and any(set(edge[2]) == {"project_id"} for edge in child_edges):
            redundant.append(child)

    assert not missing, (
        "以下表有 project_id 列但对 projects 没有覆盖 tenant_id 的外键——"
        "project_id 可以指向任意字符串，租户可以与项目租户不一致：\n  "
        + "\n  ".join(missing)
        + "\n修法：packages.shared.orm.project_tenant_fk(表名)，"
        "并写一条 ADD CONSTRAINT ... NOT VALID + VALIDATE 的迁移。"
    )
    assert not unvalidated, (
        "以下表的归属外键从未被验证（NOT VALID 只约束新写入，存量行是免检的）：\n  "
        + "\n  ".join(unvalidated)
    )
    assert not redundant, (
        "以下表同时有复合与单列 projects 外键——复合已蕴含单列，并存即两份事实源：\n  "
        + "\n  ".join(redundant)
    )


async def test_project_scoped_rows_have_consistent_tenant(admin_sessionmaker) -> None:
    """不允许孤儿行与租户不一致行（可复算的违例行数，能真 FAIL）。

    为什么必须用超级用户连接统计
    ----------------------------
    业务角色读这些表时 RLS 生效，越权行被**静默过滤**。于是「违规 0 行」可能
    只是「我看不到的那些里没有违规」——一个恒真的结论。管理连接绕过 RLS，
    数的是全表，这个数才有裁判意义。

    为什么这条不能只留在台账里
    --------------------------
    台账是**按需执行**的文档产物（人工跑脚本），CI 不跑它。数据层不变量若只
    存在于台账里，它就不是闸门，是一份快照。
    """
    async with admin_sessionmaker() as session:
        projects = await _projects_table(session)
        targets = await _derived_project_scoped_tables(session)

        orphan_report: list[str] = []
        mismatch_report: list[str] = []
        for schema, table, has_tenant in targets:
            qualified = f"{schema}.{table}"
            orphans = await session.scalar(
                text(
                    f"""
                    SELECT count(*) FROM {qualified} c
                    WHERE c.project_id IS NOT NULL
                      AND NOT EXISTS (
                          SELECT 1 FROM {projects} p WHERE p.entity_id = c.project_id
                      )
                    """
                )
            )
            if orphans:
                orphan_report.append(f"{qualified}: {orphans} 行")
            if not has_tenant:
                continue
            mismatches = await session.scalar(
                text(
                    f"""
                    SELECT count(*) FROM {qualified} c
                    JOIN {projects} p ON p.entity_id = c.project_id
                    WHERE c.tenant_id <> p.tenant_id
                    """
                )
            )
            if mismatches:
                mismatch_report.append(f"{qualified}: {mismatches} 行")

    assert not orphan_report, (
        "以下表存在指向不存在项目的孤儿行（project_id 是悬空字符串）：\n  "
        + "\n  ".join(orphan_report)
    )
    assert not mismatch_report, (
        "以下表的 tenant_id 与其项目的租户不一致"
        "（该行会出现在它不属于的租户视图里）：\n  " + "\n  ".join(mismatch_report)
    )
