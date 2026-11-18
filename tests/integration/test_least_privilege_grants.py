"""域角色授权面的运行时证据（DATA-6）：真实 ACL == 矩阵声明。

为什么这条用例是「证据」而不是「重复」：
- 静态断言（``data.role_grants_least_privilege``）证明的是「迁移会执行什么」，
  能在合并前拦住手写 GRANT；但它看不见**部署环境被手工改过**、也看不见
  `ALTER DEFAULT PRIVILEGES` 已经生效的存量对象。
- 本文件直接读 ``pg_class.relacl`` / ``pg_attribute.attacl`` / ``pg_default_acl``，
  即数据库**实际**的授权状态，与矩阵逐项比对。

两个方向都查：
1. 该有的没有（少授）→ 运行时会 permission denied，业务直接不可用；
2. 不该有的有（多授）→ 授权面宽于动作面，越权路径静默存在，审计发现不了。
第 2 类是本轮 N-06 的形态：``projects`` 有 DELETE、``organizations`` 有 INSERT，
而契约里根本没有 ``*.DELETE`` / ``ORGANIZATION.*`` 动作。
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from packages.shared.db_grants import (
    DOMAINS,
    declared_column_privileges,
    declared_privileges,
    declared_tables,
)
from packages.shared.db_roles import domain_role

# 只读 ACM 视图：pg_class.relacl / pg_attribute.attacl 是授权的**存储位置**，
# information_schema 是它的派生视图（会随当前用户的可见性被过滤）。
_TABLE_ACL = text(
    """
    SELECT n.nspname AS schema, c.relname AS name,
           a.grantee::regrole::text AS grantee, a.privilege_type
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    CROSS JOIN LATERAL aclexplode(c.relacl) a
    WHERE c.relkind IN ('r', 'p') AND a.grantee <> 0
    """
)

_COLUMN_ACL = text(
    """
    SELECT n.nspname AS schema, c.relname AS name, att.attname AS column_name,
           a.grantee::regrole::text AS grantee, a.privilege_type
    FROM pg_attribute att
    JOIN pg_class c ON c.oid = att.attrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    CROSS JOIN LATERAL aclexplode(att.attacl) a
    WHERE att.attacl IS NOT NULL AND a.grantee <> 0
    """
)

_DEFAULT_ACL = text(
    """
    SELECT n.nspname AS schema, d.defaclobjtype AS objtype,
           a.grantee::regrole::text AS grantee, a.privilege_type
    FROM pg_default_acl d
    JOIN pg_namespace n ON n.oid = d.defaclnamespace
    CROSS JOIN LATERAL aclexplode(d.defaclacl) a
    WHERE a.grantee <> 0
    """
)


async def _table_acl(admin_sessionmaker) -> dict[tuple[str, str, str], set[str]]:
    """``(role, schema, table) -> 权限集``（整表级）。"""
    async with admin_sessionmaker() as session:
        rows = (await session.execute(_TABLE_ACL)).all()
    grants: dict[tuple[str, str, str], set[str]] = {}
    for row in rows:
        grants.setdefault((row[2], row[0], row[1]), set()).add(row[3])
    return grants


async def _column_acl(
    admin_sessionmaker,
) -> dict[tuple[str, str, str], tuple[set[str], set[str]]]:
    """``(role, schema, table) -> (列集合, 权限集)``（列级授权）。"""
    async with admin_sessionmaker() as session:
        rows = (await session.execute(_COLUMN_ACL)).all()
    grants: dict[tuple[str, str, str], tuple[set[str], set[str]]] = {}
    for row in rows:
        columns, privileges = grants.setdefault((row[3], row[0], row[1]), (set(), set()))
        columns.add(row[2])
        privileges.add(row[4])
    return grants


async def test_domain_role_privileges_match_matrix(admin_sessionmaker) -> None:
    """真实 ACL 必须与授权矩阵逐项相等（少授与多授都算失败）。

    比对范围严格限定在矩阵声明过的表上：矩阵是「该角色**应该**拥有什么」的
    唯一声明，矩阵之外的表上出现任何权限都属于多授。
    """
    actual = await _table_acl(admin_sessionmaker)
    mismatches: list[str] = []

    for domain in DOMAINS:
        role = domain_role(domain)
        expected = {
            (schema, table): privileges
            for (schema, table), privileges in declared_privileges(domain).items()
        }
        # 只取该角色在这些被监视表上的实际权限。
        watched = declared_tables(domain)
        observed = {
            (schema, table): privileges
            for (grantee, schema, table), privileges in actual.items()
            if grantee == role and (schema, table) in watched
        }

        for key in sorted(set(expected) | set(observed)):
            want = expected.get(key, frozenset())
            got = frozenset(observed.get(key, frozenset()))
            if want != got:
                mismatches.append(
                    f"{role} 对 {key[0]}.{key[1]}: 实际={sorted(got) or '无'}，"
                    f"矩阵={sorted(want) or '无'}"
                )

        # 业务 schema 里出现了矩阵之外的表授权 → 新表未经声明就获得了权限。
        overreach = sorted(
            (schema, table)
            for (grantee, schema, table) in actual
            if grantee == role and schema == domain and (schema, table) not in watched
        )
        for schema, table in overreach:
            mismatches.append(
                f"{role} 在未声明的表 {schema}.{table} 上持有权限"
                "（新表必须先进入 db_grants.py 矩阵）"
            )

    assert not mismatches, "域角色授权与矩阵不一致：\n" + "\n".join(mismatches)


async def test_cross_domain_read_is_column_scoped(admin_sessionmaker) -> None:
    """资产域对 projects 的跨域只读必须**列级**（授整表等于把未来所有列一起暴露）。"""
    column_grants = await _column_acl(admin_sessionmaker)
    role = domain_role("asset_service")
    columns, privileges = column_grants.get((role, "project_planning", "projects"), (set(), set()))

    expected_columns, expected_privileges = declared_column_privileges("asset_service")[
        ("project_planning", "projects")
    ]
    assert columns == set(expected_columns), (
        f"{role} 对 project_planning.projects 的列级授权={sorted(columns)}，"
        f"矩阵={sorted(expected_columns)}"
    )
    assert privileges == set(expected_privileges)

    # 列级之外不得再叠加整表授权（否则列级收窄毫无意义）。
    table_grants = await _table_acl(admin_sessionmaker)
    assert ("project_planning", "projects") not in {
        (schema, table)
        for (grantee, schema, table) in table_grants
        if grantee == role
    }, "资产域对 projects 持有整表授权：列级只读被整表 SELECT 覆盖，等于没做列级"


async def test_no_default_privileges_on_business_schemas(admin_sessionmaker) -> None:
    """业务 schema 不得给域角色留默认权限（否则「下一次建表」自动带权限）。

    这正是 N-06 的第二个根因：``b7c8d9e0f1a2`` 的「默认零授权」只 REVOKE 了
    ``app_role``，而 ``8f3b2c1d4e5a`` 又给两个域角色授了 ``GRANT SELECT, INSERT,
    UPDATE, DELETE ON TABLES`` 的默认权限——旧机制在新角色上原样复发。
    """
    async with admin_sessionmaker() as session:
        rows = (await session.execute(_DEFAULT_ACL)).all()
    roles = {domain_role(domain) for domain in DOMAINS}
    leftovers = [
        (row[0], row[1], row[2], row[3])
        for row in rows
        if row[2] in roles and row[0] in DOMAINS
    ]
    assert not leftovers, (
        "业务 schema 仍存在域角色的默认权限（新表会自动获得，fail-closed 失效）："
        f"{leftovers}"
    )


@pytest.mark.parametrize(
    "statement",
    [
        "DELETE FROM project_planning.projects WHERE false",
        "DELETE FROM project_planning.episodes WHERE false",
        "DELETE FROM project_planning.script_revisions WHERE false",
    ],
)
async def test_project_role_cannot_delete(db_sessionmaker, statement: str) -> None:
    """``DELETE`` 一律被拒（42501）：删除语义是 ARCHIVED/SUPERSEDED，不是物理删除。

    用 ``WHERE false`` 而不是真有数据：权限检查发生在**扫描之前**，因此不需要
    构造可删行——这也让用例不依赖任何既有数据，跑到哪个库上都成立。
    """
    async with db_sessionmaker() as session:
        with pytest.raises(DBAPIError) as exc_info:
            await session.execute(text(statement))
        assert "42501" in str(exc_info.value) or "permission denied" in str(
            exc_info.value
        ).lower()
        await session.rollback()


@pytest.mark.parametrize(
    "statement",
    [
        # 组织是平台级归属根：契约里没有 ORGANIZATION.* 动作前只有 SELECT。
        "INSERT INTO project_planning.organizations "
        "(organization_id, tenant_id, name, created_at, created_by, updated_at) "
        "VALUES (gen_random_uuid(), gen_random_uuid(), 'x', now(), 't', now())",
        "UPDATE project_planning.organizations SET name = 'x' WHERE false",
        "DELETE FROM project_planning.organizations WHERE false",
    ],
)
async def test_organizations_are_read_only(db_sessionmaker, statement: str) -> None:
    """``organizations`` 对业务域角色只读：写入必须经管理面（ORGANIZATION.* 动作）。"""
    async with db_sessionmaker() as session:
        with pytest.raises(DBAPIError) as exc_info:
            await session.execute(text(statement))
        assert "42501" in str(exc_info.value) or "permission denied" in str(
            exc_info.value
        ).lower()
        await session.rollback()


async def test_asset_role_cannot_delete_asset_versions(asset_sessionmaker) -> None:
    """资产不可覆盖（红线）：DELETE 在数据库层被拒，而不是靠服务层自觉。"""
    async with asset_sessionmaker() as session:
        with pytest.raises(DBAPIError) as exc_info:
            await session.execute(text("DELETE FROM asset_service.asset_versions WHERE false"))
        assert "42501" in str(exc_info.value) or "permission denied" in str(
            exc_info.value
        ).lower()
        await session.rollback()


async def test_qc_reports_are_append_only(asset_sessionmaker) -> None:
    """质检结论是证据：只能 INSERT/SELECT，UPDATE 在数据库层被拒。"""
    async with asset_sessionmaker() as session:
        with pytest.raises(DBAPIError) as exc_info:
            await session.execute(
                text(
                    "UPDATE asset_service.qc_reports "
                    "SET human_decision = 'APPROVED' WHERE false"
                )
            )
        assert "42501" in str(exc_info.value) or "permission denied" in str(
            exc_info.value
        ).lower()
        await session.rollback()


async def test_asset_role_cannot_read_unscoped_project_columns(
    asset_sessionmaker,
) -> None:
    """跨域只读被限定在归属三列：读 ``project_name`` 必须被拒。

    这条用例守的是「列级」这个形态本身——若哪天有人把列级改成整表 SELECT，
    前一条用例（列清单相等）会报红，本用例会静默通过。两条一起才说明
    「不是恰好清单一样，而是真的只能读这三列」。
    """
    async with asset_sessionmaker() as session:
        with pytest.raises(DBAPIError) as exc_info:
            await session.execute(
                text("SELECT project_name FROM project_planning.projects LIMIT 1")
            )
        assert "42501" in str(exc_info.value) or "permission denied" in str(
            exc_info.value
        ).lower()
        await session.rollback()


async def test_organization_seed_requires_admin(admin_sessionmaker) -> None:
    """管理连接能写组织（否则上面的「只读」就没有正向对照）。

    没有正向对照的负向用例无法区分「权限收紧生效」与「环境根本写不了」——
    后者会让所有负向断言都通过，包括权限被放宽的那些。
    """
    org_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    async with admin_sessionmaker() as session:
        await session.execute(
            text(
                """
                INSERT INTO project_planning.organizations
                    (organization_id, tenant_id, name, created_at, created_by, updated_at)
                VALUES (:org, :tid, '管理面组织', now(), 'admin', now())
                """
            ),
            {"org": org_id, "tid": tenant_id},
        )
        await session.rollback()
