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
         :now, 'tester', :now, :pid, 'IMAGE', :sha, '{}'::jsonb, '[]'::jsonb)
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
