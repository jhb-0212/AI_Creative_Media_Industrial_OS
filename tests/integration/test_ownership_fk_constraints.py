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

依赖真实 PostgreSQL，无 PG 环境时本文件全部跳过（CI 有 PG 时执行）。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError


def _now() -> datetime:
    return datetime.now(UTC)


async def test_project_must_reference_existing_organization(db_sessionmaker) -> None:
    """写引用不存在组织的项目 → 外键约束拒绝（SCOPE-1）。"""
    sm = db_sessionmaker
    nonexistent_org = uuid.uuid4()
    tenant = uuid.uuid4()

    async with sm() as session:
        await session.execute(
            text("SELECT set_config('app.current_tenant_id', :t, true)"),
            {"t": str(tenant)},
        )
        with pytest.raises(IntegrityError):
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
        await session.rollback()


async def test_episode_must_reference_existing_project(db_sessionmaker) -> None:
    """写引用不存在项目的集数 → 外键约束拒绝（SCOPE-2）。"""
    sm = db_sessionmaker
    tenant = uuid.uuid4()
    nonexistent_project_id = uuid.uuid4().hex

    async with sm() as session:
        await session.execute(
            text("SELECT set_config('app.current_tenant_id', :t, true)"),
            {"t": str(tenant)},
        )
        with pytest.raises(IntegrityError):
            await session.execute(
                text(
                    """
                    INSERT INTO project_planning.episodes
                        (entity_id, entity_type, schema_version, entity_version,
                         status, owner_module, tenant_id, created_at, created_by,
                         updated_at, project_id, episode_ref, title)
                    VALUES
                        (:eid, 'Episode', 205, 1, 'DRAFT', 'project_planning',
                         :tid, :now, 'tester', :now, :pid, 'EP01', '悬空集数')
                    """
                ),
                {
                    "eid": uuid.uuid4().hex,
                    "tid": tenant,
                    "now": _now(),
                    "pid": nonexistent_project_id,
                },
            )
        await session.rollback()


async def test_project_with_valid_organization_succeeds(db_sessionmaker) -> None:
    """正向：先建组织再建归属项目 → 外键约束放行（SCOPE-1 不误伤合法路径）。"""
    sm = db_sessionmaker
    tenant = uuid.uuid4()
    org_id = uuid.uuid4()

    async with sm() as session:
        await session.execute(
            text("SELECT set_config('app.current_tenant_id', :t, true)"),
            {"t": str(tenant)},
        )
        # 1. 建组织。
        await session.execute(
            text(
                """
                INSERT INTO project_planning.organizations
                    (organization_id, tenant_id, name, created_at, created_by, updated_at)
                VALUES (:org, :tid, '测试组织', :now, 'tester', :now)
                """
            ),
            {"org": org_id, "tid": tenant, "now": _now()},
        )
        # 2. 建归属该组织的项目（不抛异常即通过）。
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


async def test_project_cannot_reference_cross_tenant_organization(
    db_sessionmaker,
) -> None:
    """项目引用**另一租户**的组织 → 复合外键拒绝（审查 P1-03）。

    这是单列外键漏掉的那一类：组织真实存在，但属于租户 A，而项目写进
    租户 B。只有把 tenant_id 纳入外键，数据库才能拒绝这种归属错配。
    """
    sm = db_sessionmaker
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    org_in_a = uuid.uuid4()

    async with sm() as session:
        # 1. 在租户 A 建组织（A 的上下文内）。
        await session.execute(
            text("SELECT set_config('app.current_tenant_id', :t, true)"),
            {"t": str(tenant_a)},
        )
        await session.execute(
            text(
                """
                INSERT INTO project_planning.organizations
                    (organization_id, tenant_id, name, created_at, created_by, updated_at)
                VALUES (:org, :tid, 'A 租户组织', :now, 'tester', :now)
                """
            ),
            {"org": org_in_a, "tid": tenant_a, "now": _now()},
        )

        # 2. 切到租户 B，写入引用 A 组织的项目 → 必须被复合外键拒绝。
        await session.execute(
            text("SELECT set_config('app.current_tenant_id', :t, true)"),
            {"t": str(tenant_b)},
        )
        with pytest.raises(IntegrityError):
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
        await session.rollback()


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
        await session.execute(
            text("SELECT set_config('app.current_tenant_id', :t, true)"),
            {"t": str(tenant)},
        )
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


async def test_asset_must_reference_existing_project(asset_sessionmaker) -> None:
    """资产引用不存在项目 → 复合外键拒绝（SCOPE-3 / 第二轮审查 P1-04）。

    这是 P1-04 的可执行证明：整改前模型里根本没有 project_id，资产无法表达
    归属；现在「挂着不存在的项目」在数据库层就写不进去。
    """
    tenant = uuid.uuid4()
    async with asset_sessionmaker() as session:
        await session.execute(
            text("SELECT set_config('app.current_tenant_id', :t, true)"),
            {"t": str(tenant)},
        )
        with pytest.raises(IntegrityError):
            await session.execute(
                text(_ASSET_INSERT),
                {
                    "eid": uuid.uuid4().hex,
                    "tid": tenant,
                    "now": _now(),
                    "pid": uuid.uuid4().hex,
                    "sha": "a" * 64,
                },
            )
        await session.rollback()


async def test_asset_cannot_reference_cross_tenant_project(
    db_sessionmaker, asset_sessionmaker
) -> None:
    """资产引用**另一租户**的项目 → 复合外键拒绝（单列外键漏掉的那一类）。"""
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    project_in_a = uuid.uuid4().hex
    await _insert_project(db_sessionmaker, project_id=project_in_a, tenant=tenant_a)

    async with asset_sessionmaker() as session:
        # 切到租户 B 写资产，指向租户 A 的项目 → 必须被拒。
        await session.execute(
            text("SELECT set_config('app.current_tenant_id', :t, true)"),
            {"t": str(tenant_b)},
        )
        with pytest.raises(IntegrityError):
            await session.execute(
                text(_ASSET_INSERT),
                {
                    "eid": uuid.uuid4().hex,
                    "tid": tenant_b,
                    "now": _now(),
                    "pid": project_in_a,
                    "sha": "b" * 64,
                },
            )
        await session.rollback()


async def test_asset_with_same_tenant_project_succeeds(
    db_sessionmaker, asset_sessionmaker
) -> None:
    """正向：同租户真实项目下登记资产 → 外键放行（不误伤合法路径）。"""
    tenant = uuid.uuid4()
    project_id = uuid.uuid4().hex
    await _insert_project(db_sessionmaker, project_id=project_id, tenant=tenant)

    async with asset_sessionmaker() as session:
        await session.execute(
            text("SELECT set_config('app.current_tenant_id', :t, true)"),
            {"t": str(tenant)},
        )
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


async def test_organizations_are_tenant_isolated(db_sessionmaker) -> None:
    """organizations 表纳入租户 RLS：别的租户看不到本租户的组织（审查 R8）。"""
    sm = db_sessionmaker
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    org_in_a = uuid.uuid4()

    async with sm() as session:
        await session.execute(
            text("SELECT set_config('app.current_tenant_id', :t, true)"),
            {"t": str(tenant_a)},
        )
        await session.execute(
            text(
                """
                INSERT INTO project_planning.organizations
                    (organization_id, tenant_id, name, created_at, created_by, updated_at)
                VALUES (:org, :tid, 'A 租户组织', :now, 'tester', :now)
                """
            ),
            {"org": org_in_a, "tid": tenant_a, "now": _now()},
        )

        # A 上下文可见。
        visible_a = await session.scalar(
            text(
                "SELECT count(*) FROM project_planning.organizations "
                "WHERE organization_id = :org"
            ),
            {"org": org_in_a},
        )
        assert visible_a == 1

        # B 上下文不可见（RLS 过滤）。
        await session.execute(
            text("SELECT set_config('app.current_tenant_id', :t, true)"),
            {"t": str(tenant_b)},
        )
        visible_b = await session.scalar(
            text(
                "SELECT count(*) FROM project_planning.organizations "
                "WHERE organization_id = :org"
            ),
            {"org": org_in_a},
        )
        assert visible_b == 0
        await session.rollback()
