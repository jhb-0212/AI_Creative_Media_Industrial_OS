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

判据覆盖**三类授权路径**（缺一类就有一种越权静默通过）：
- 表级（``pg_class.relacl``）：整表 DML；
- 列级（``pg_attribute.attacl``）：``GRANT UPDATE (col)`` 不进表级 ACL，
  只看表级会把「能改一列」判成「无权限」——append-only 表上最隐蔽的一条；
- 角色继承（``pg_auth_members``）：权限记在组角色名下，成员继承；只比对
  ``grantee = 域角色`` 会把组上的越权判成「矩阵没有、库里也没有」。
三类共用同一份矩阵声明与同一批 ACM 查询，不另建第二套期望值。
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
    referenced_schemas,
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

# ``PUBLIC`` 是 grantee OID 0，``grantee::regrole`` 对它没有名字（会报错），
# 所以只能单独查、只取权限类型。为什么要单独查：``PUBLIC`` 的授权对**所有**
# 角色生效，却不会出现在「按角色过滤」的结果里——按角色比对越权永远看不见它
# （评审 F4）。收敛迁移现在也 ``REVOKE … FROM PUBLIC``，两侧都要有证据。
_PUBLIC_TABLE_ACL = text(
    """
    SELECT n.nspname AS schema, c.relname AS name, a.privilege_type
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    CROSS JOIN LATERAL aclexplode(c.relacl) a
    WHERE c.relkind IN ('r', 'p') AND a.grantee = 0
    """
)

# 角色成员关系（PG 的权限可以挂在**组角色**名下，成员通过本表继承）。
# 只按 ``grantee = 域角色`` 过滤 ACL，等于假设「没有组成员关系」；一旦有人
# 用组角色收口权限，越权会藏在组的那一行里，按角色过滤永远看不见。
_ROLE_MEMBERSHIPS = text(
    """
    SELECT m.member::regrole::text AS member,
           m.roleid::regrole::text AS group_role
    FROM pg_auth_members m
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


async def _existing_tables(admin_sessionmaker) -> set[tuple[str, str]]:
    """当前库里真实存在的表（``to_regclass`` 的等价派生）。

    「对象不存在」与「权限为空」是两种事实：前者是本次对账不适用（分叉链对象
    未进入本库），后者是授权漂移。把缺表算成漂移会引导人手写 GRANT 去补一张
    不存在的表，反而把授权矩阵变成迁移链问题的遮羞布。
    """
    sql = text(
        """
        SELECT n.nspname AS schema, c.relname AS name
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind IN ('r', 'p')
        """
    )
    async with admin_sessionmaker() as session:
        rows = (await session.execute(sql)).all()
    return {(row[0], row[1]) for row in rows}


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


async def _role_memberships(
    admin_sessionmaker,
) -> dict[str, set[str]]:
    """``成员角色 -> 它直接继承的组角色集合``（传递闭包在 Python 里算）。"""
    async with admin_sessionmaker() as session:
        rows = (await session.execute(_ROLE_MEMBERSHIPS)).all()
    memberships: dict[str, set[str]] = {}
    for member, group_role in rows:
        memberships.setdefault(member, set()).add(group_role)
    return memberships


def _privilege_sources(role: str, memberships: dict[str, set[str]]) -> set[str]:
    """该角色的**有效授权来源**：自身 + 直接/间接所属的组角色。

    为什么必须展开：``relacl`` / ``attacl`` 记录的是「授给了谁」。域角色若通过
    组成员关系继承权限，那条授权记在组名下；只比对 ``grantee = 域角色`` 的行，
    会把「域角色实际能写」判成「矩阵没有、库里也没有」——漏报，而且是静默的。
    """
    sources = {role}
    pending = [role]
    while pending:
        member = pending.pop()
        for group_role in memberships.get(member, ()):
            if group_role not in sources:
                sources.add(group_role)
                pending.append(group_role)
    return sources


def _effective_table_grants(
    actual: dict[tuple[str, str, str], set[str]],
    sources: set[str],
    watched: frozenset[tuple[str, str]],
) -> dict[tuple[str, str], set[str]]:
    """把闭包内各来源的表级授权并成「该角色实际持有的权限集」。"""
    merged: dict[tuple[str, str], set[str]] = {}
    for (grantee, schema, table), privileges in actual.items():
        if grantee not in sources or (schema, table) not in watched:
            continue
        merged.setdefault((schema, table), set()).update(privileges)
    return merged


def _effective_column_grants(
    actual: dict[tuple[str, str, str], tuple[set[str], set[str]]],
    sources: set[str],
) -> dict[tuple[str, str], tuple[set[str], set[str]]]:
    """把闭包内各来源的列级授权并成「该角色实际可读写的列与权限」。"""
    merged: dict[tuple[str, str], tuple[set[str], set[str]]] = {}
    for (grantee, schema, table), (columns, privileges) in actual.items():
        if grantee not in sources:
            continue
        cols, privs = merged.setdefault((schema, table), (set(), set()))
        cols.update(columns)
        privs.update(privileges)
    return merged


async def test_domain_role_privileges_match_matrix(admin_sessionmaker) -> None:
    """真实 ACL 必须与授权矩阵逐项相等（少授与多授都算失败）。

    判据必须覆盖三类授权路径，缺一类就会出现「矩阵对、库里越权、测试绿」：

    1. **表级**（``pg_class.relacl``）：整表 DML；
    2. **列级**（``pg_attribute.attacl``）：只授某几列。``GRANT UPDATE (col)``
       不会出现在表级 ACL 里，只看表级会把这类越权判成「无权限」——在
       append-only 表上这是最隐蔽的一条；
    3. **角色继承**（``pg_auth_members``）：权限记在组角色名下，成员继承。
       只比对 ``grantee = 域角色`` 会把组上的越权判成「矩阵没有、库里也没有」。

    比对范围严格限定在矩阵声明过且当前库真实存在的对象上：矩阵是「该角色
    **应该**拥有什么」的唯一声明，矩阵之外出现任何权限都属于多授。
    分叉链对象未进入当前库时单列 N/A，不把「对象不存在」误判成「权限为空」。
    """
    table_actual = await _table_acl(admin_sessionmaker)
    column_actual = await _column_acl(admin_sessionmaker)
    memberships = await _role_memberships(admin_sessionmaker)
    existing = await _existing_tables(admin_sessionmaker)
    mismatches: list[str] = []
    not_applicable: list[str] = []

    for domain in DOMAINS:
        role = domain_role(domain)
        sources = _privilege_sources(role, memberships)

        # ---- 表级：整表授权 ----
        expected_all = dict(declared_privileges(domain))
        # 可机检前提：只有真表存在才参与 ACL 对账；缺表单列 N/A，不计通过率。
        expected = {key: value for key, value in expected_all.items() if key in existing}
        not_applicable.extend(
            f"{role} 对 {schema}.{table}（对象不存在）"
            for schema, table in sorted(set(expected_all) - set(expected))
        )
        # 只取该角色（含其继承的组）在这些被监视表上的实际权限。
        watched = declared_tables(domain)
        observed = _effective_table_grants(table_actual, sources, watched)

        for key in sorted(set(expected) | set(observed)):
            want = expected.get(key, frozenset())
            got = frozenset(observed.get(key, frozenset()))
            if want != got:
                mismatches.append(
                    f"[表级] {role} 对 {key[0]}.{key[1]}: 实际={sorted(got) or '无'}，"
                    f"矩阵={sorted(want) or '无'}"
                )

        # 业务 schema 里出现了矩阵之外的表授权 → 新表未经声明就获得了权限。
        overreach = sorted(
            (schema, table)
            for (grantee, schema, table) in table_actual
            if grantee in sources and schema == domain and (schema, table) not in watched
        )
        for schema, table in overreach:
            mismatches.append(
                f"[表级] {role} 在未声明的表 {schema}.{table} 上持有权限"
                "（新表必须先进入 db_grants.py 矩阵）"
            )

        # ---- 列级：只授某些列 ----
        expected_columns_all = declared_column_privileges(domain)
        expected_columns = {
            key: value for key, value in expected_columns_all.items() if key in existing
        }
        not_applicable.extend(
            f"{role} 对 {schema}.{table} 的列级授权（对象不存在）"
            for schema, table in sorted(set(expected_columns_all) - set(expected_columns))
        )
        observed_columns = _effective_column_grants(column_actual, sources)

        for key in sorted(set(expected_columns) | set(observed_columns)):
            want_columns, want_privileges = expected_columns.get(key, (frozenset(), frozenset()))
            got_columns, got_privileges = observed_columns.get(key, (set(), set()))
            if set(want_columns) != got_columns or set(want_privileges) != got_privileges:
                mismatches.append(
                    f"[列级] {role} 对 {key[0]}.{key[1]}: "
                    f"实际列={sorted(got_columns) or '无'} "
                    f"权限={sorted(got_privileges) or '无'}，"
                    f"矩阵列={sorted(want_columns) or '无'} "
                    f"权限={sorted(want_privileges) or '无'}"
                )

    if not_applicable:
        print("[N-A] ACL 对账跳过不存在的对象：" + "、".join(not_applicable))
    assert not mismatches, "域角色授权与矩阵不一致：\n" + "\n".join(mismatches)


_SCHEMA_USAGE = text(
    """
    SELECT n.nspname AS schema,
           has_schema_privilege(r.oid, n.oid, 'USAGE') AS has_usage
    FROM pg_namespace n CROSS JOIN pg_roles r
    WHERE r.rolname = :role AND n.nspname = ANY(:schemas)
    """
)


async def test_domain_role_has_usage_on_every_referenced_schema(
    admin_sessionmaker,
) -> None:
    """矩阵里被授过表权限的每个 schema，该域角色都必须持有 USAGE。

    为什么单列一条守卫：schema USAGE 与表权限存在**两个不同的 ACL 位置**
    （``pg_namespace.nspacl`` / ``pg_class.relacl``），上面那条 ACL 对账只比表级，
    结构上永远看不见 USAGE 缺失。而缺 USAGE 的表现是「表权限明明有，第一次读写
    就 42501 permission denied for schema」——授权面看着全对，功能整体不可用。

    判据集合从矩阵派生（:func:`referenced_schemas`），不写死 outbox/audit/auth：
    将来某个域开始写一张新的共享表，这条守卫自动把它纳入。
    """
    required = {domain: referenced_schemas(domain) for domain in DOMAINS}
    all_schemas = sorted({schema for schemas in required.values() for schema in schemas})

    async with admin_sessionmaker() as session:
        missing: list[str] = []
        for domain in DOMAINS:
            role = domain_role(domain)
            rows = (
                await session.execute(
                    _SCHEMA_USAGE, {"role": role, "schemas": list(required[domain])}
                )
            ).all()
            seen = {row[0]: row[1] for row in rows}
            # 「schema 不存在」与「USAGE 为空」必须分开：把不存在算成缺 USAGE
            # 会引导人去 GRANT 一个不存在的 schema，而真正的错在别处。
            for schema in required[domain]:
                if schema not in seen:
                    missing.append(f"{role}: schema {schema} 不存在（无法判定，需先建 schema）")
                elif not seen[schema]:
                    missing.append(
                        f"{role} 对 schema {schema} 无 USAGE（矩阵在此有表权限："
                        f"{sorted(t for s, t in declared_tables(domain) if s == schema)}）"
                    )

    assert not missing, (
        "schema USAGE 缺失：只授表权限不授 USAGE，运行时会 42501 且 ACL 对账看不出来\n"
        + "\n".join(missing)
        + f"\n（检查范围共 {len(all_schemas)} 个 schema：{all_schemas}）"
    )


async def test_business_tables_grant_nothing_to_public(admin_sessionmaker) -> None:
    """业务表不得对 ``PUBLIC`` 授任何权限（评审 F4）。

    只按域角色比对会漏掉这一整类越权：``GRANT UPDATE ON t TO PUBLIC`` 让**所有**
    角色（含域角色）都能写，而按角色过滤的 ACL 结果里根本没有这一条——比对会
    PASS，权限却是宽的。``grantee = 0`` 就是 ``PUBLIC``。
    """
    async with admin_sessionmaker() as session:
        rows = (await session.execute(_PUBLIC_TABLE_ACL)).all()
    offenders = [f"{row[0]}.{row[1]}: {row[2]}" for row in rows if row[0] in DOMAINS]
    assert not offenders, "业务表对 PUBLIC 授权（所有角色都会继承，矩阵形同虚设）：\n" + "\n".join(
        offenders
    )


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
        (schema, table) for (grantee, schema, table) in table_grants if grantee == role
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
        (row[0], row[1], row[2], row[3]) for row in rows if row[2] in roles and row[0] in DOMAINS
    ]
    assert not leftovers, (
        f"业务 schema 仍存在域角色的默认权限（新表会自动获得，fail-closed 失效）：{leftovers}"
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
        assert "42501" in str(exc_info.value) or "permission denied" in str(exc_info.value).lower()
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
        assert "42501" in str(exc_info.value) or "permission denied" in str(exc_info.value).lower()
        await session.rollback()


async def test_asset_role_cannot_delete_asset_versions(asset_sessionmaker) -> None:
    """资产不可覆盖（红线）：DELETE 在数据库层被拒，而不是靠服务层自觉。"""
    async with asset_sessionmaker() as session:
        with pytest.raises(DBAPIError) as exc_info:
            await session.execute(text("DELETE FROM asset_service.asset_versions WHERE false"))
        assert "42501" in str(exc_info.value) or "permission denied" in str(exc_info.value).lower()
        await session.rollback()


async def test_qc_reports_are_append_only(asset_sessionmaker) -> None:
    """质检结论是证据：只能 INSERT/SELECT，UPDATE 在数据库层被拒。"""
    async with asset_sessionmaker() as session:
        with pytest.raises(DBAPIError) as exc_info:
            await session.execute(
                text("UPDATE asset_service.qc_reports SET human_decision = 'APPROVED' WHERE false")
            )
        assert "42501" in str(exc_info.value) or "permission denied" in str(exc_info.value).lower()
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
        assert "42501" in str(exc_info.value) or "permission denied" in str(exc_info.value).lower()
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
