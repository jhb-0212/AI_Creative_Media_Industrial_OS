"""两个遗留壳角色的退役工具：``afs_audit`` / ``afs_data_repair``。

为什么要「先证明没人用」再 DROP
------------------------------
``DROP ROLE`` 的作用域是**集群**，不是当前库。只要任意一个数据库里还有
「授予该角色的表权限」「``TO afs_audit`` 的策略」「以它为主键的成员关系」，
PostgreSQL 就会拒绝删除并列出依赖。更麻烦的是它拒得**不完整**：错误信息只报
第一个阻塞对象，删完一处再跑一次才看到下一处。所以这里一次性把全部引用面
枚举出来再决定，而不是「试着 DROP、失败就 REVOKE 一条」。

这两条角色在本仓库的主迁移链里**从未被创建**（``data/migrations/versions``
零命中），干净库（``ai_film_pg16_clean`` / ``ai_film_pg18_repair``）里也根本
不存在——它们只存在于某些开发集群上，是 ``data/migrations/pending/feat-ownership``
那套未合并设计的手工实验残留。因此退役不是「拆掉一套在用的能力」，而是
「把未落地的设计留下的壳从集群里拿走」。

判定口径（全部机器可查，不靠人回忆）
------------------------------------
对每个数据库分别枚举六类引用：

1. ``pg_policies.roles`` 里直接出现该角色；
2. 策略表达式里出现角色名——角色一删，含 ``pg_has_role(..., '<角色>', ...)``
   的表达式会在每次查询时报错，所以它同样是硬阻塞；
3. 表 / 视图的 ACL（``pg_class.relacl``）；
4. schema 的 ACL（``pg_namespace.nspacl``）——USAGE 也算引用；
5. 函数 / 过程的 ACL（``pg_proc.proacl``）；
6. 默认权限（``pg_default_acl``）、对象属主（``relowner`` / ``nspowner``）、
   成员关系（``pg_auth_members``，两个方向）。

另有一条**权威交叉校验**：``pg_shdepend`` 里 ``refclassid = pg_authid`` 的行
就是 ``DROP ROLE`` 实际会检查的那张依赖表。上面六类是为了给出「可读的定位」，
``pg_shdepend`` 是为了给出「删得掉吗」的最终答案——两者不一致时以它为准，
这也是 ``assess`` 把「看起来干净」与「真的干净」分开判的原因。

用法::

    python scripts/retire_legacy_roles.py --check     # 只报告，退出码 0=可删 / 2=有阻塞
    python scripts/retire_legacy_roles.py --drop      # 仅在「全集群零引用」时真正 DROP

``--drop`` 刻意不做「顺手清理」：遇到阻塞就原样退出并打印清单，把「删开发集群
实验库里的历史授权」留给人的一次显式决定。一个会自己 REVOKE 一批表的工具，等于
把「退役一个空壳」偷偷升级成「改动若干库的授权面」。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict, dataclass

import asyncpg  # type: ignore[import-untyped]  # 无 py.typed；本工具只用到 connect/fetch/execute

from packages.shared.config import get_settings

LEGACY_ROLES: tuple[str, ...] = ("afs_audit", "afs_data_repair")

# 模板库没有业务对象；其余库逐一检查（空库也要看：空库同样可能存 ACL）。
_EXCLUDED_DATABASES = frozenset({"template0", "template1"})
# 维护连接：``DROP ROLE`` 与 ``pg_shdepend`` 都是集群级操作，任选一个库即可。
_MAINTENANCE_DATABASE = "postgres"

EXIT_CLEAN = 0
EXIT_BLOCKED = 2
EXIT_DROP_FAILED = 4


@dataclass(frozen=True)
class Reference:
    """一条「谁在引用这个角色」的可读记录（用于定位，不用于最终判定）。"""

    database: str
    role: str
    kind: str
    detail: str


@dataclass(frozen=True)
class Assessment:
    """退役判定：可删 ⟺ 角色存在且全集群零引用。"""

    existing: tuple[str, ...]
    references: tuple[Reference, ...]
    shadow_dependencies: tuple[tuple[str, int], ...]

    @property
    def deletable(self) -> bool:
        """只为「存在且零引用」的角色返回 True。

        不区分引用来自本库还是别的库：``DROP ROLE`` 本来就是集群级，只要存在
        一条引用，删除就会失败。

        两个来源都要空：``pg_shdepend`` 是权威判据，可读引用是定位手段。二者
        不一致（可读到、影子依赖为空）时同样判阻塞——宁可多要人看一眼，也不
        要在信息矛盾时放行 DROP。
        """
        return bool(self.existing) and not self.shadow_dependencies and not self.references


def assess(
    *,
    existing: tuple[str, ...],
    references: tuple[Reference, ...],
    shadow_dependencies: tuple[tuple[str, int], ...],
) -> Assessment:
    """纯判定（可单测）：把「取数」与「结论」分开。

    ``shadow_dependencies`` 是 ``pg_shdepend`` 的 ``(database, count)`` 汇总。
    它非空即不可删，哪怕 ``references`` 为空——那说明还有一类引用没被上面
    六类查询覆盖（例如列级授权），此时**不能**因为「看起来干净」就放行。
    """
    return Assessment(
        existing=existing,
        references=references,
        shadow_dependencies=shadow_dependencies,
    )


def _dsn(database: str) -> dict[str, object]:
    settings = get_settings()
    return {
        "host": settings.postgres_host,
        "port": settings.postgres_port,
        "user": settings.postgres_user,
        "password": settings.postgres_password.get_secret_value(),
        "database": database,
    }


_SQL_DATABASES = "SELECT datname FROM pg_database WHERE NOT datistemplate ORDER BY 1"

_SQL_EXISTING = "SELECT rolname FROM pg_roles WHERE rolname = ANY($1::text[]) ORDER BY 1"

# 每条查询都自带 ``role`` 列：把「对象 + 权限」与「是哪个角色」放在同一行，
# 避免事后用字符串匹配去猜归属（那会把对象名里含角色名的表误判成引用）。
_SQL_POLICY_ROLES = """
SELECT n.nspname || '.' || c.relname AS object,
       p.polname AS policy,
       role_oid::regrole::text AS role
  FROM pg_policy p
  JOIN pg_class c ON c.oid = p.polrelid
  JOIN pg_namespace n ON n.oid = c.relnamespace
  JOIN LATERAL unnest(p.polroles) AS role_oid ON true
 WHERE role_oid::regrole::text = ANY($1::text[])
"""

_SQL_POLICY_EXPR = """
SELECT n.nspname || '.' || c.relname AS object,
       p.polname AS policy,
       t.role
  FROM pg_policy p
  JOIN pg_class c ON c.oid = p.polrelid
  JOIN pg_namespace n ON n.oid = c.relnamespace,
       LATERAL unnest($1::text[]) AS t(role)
 WHERE coalesce(pg_get_expr(p.polqual, p.polrelid), '') LIKE '%' || t.role || '%'
    OR coalesce(pg_get_expr(p.polwithcheck, p.polrelid), '') LIKE '%' || t.role || '%'
"""

_SQL_CLASS_ACL = """
SELECT n.nspname || '.' || c.relname AS object,
       a.privilege_type AS privilege,
       pg_get_userbyid(a.grantee) AS role
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace,
       LATERAL aclexplode(c.relacl) a
 WHERE pg_get_userbyid(a.grantee) = ANY($1::text[])
"""

_SQL_NAMESPACE_ACL = """
SELECT n.nspname AS object,
       a.privilege_type AS privilege,
       pg_get_userbyid(a.grantee) AS role
  FROM pg_namespace n, LATERAL aclexplode(n.nspacl) a
 WHERE pg_get_userbyid(a.grantee) = ANY($1::text[])
"""

_SQL_PROC_ACL = """
SELECT n.nspname || '.' || p.proname AS object,
       a.privilege_type AS privilege,
       pg_get_userbyid(a.grantee) AS role
  FROM pg_proc p
  JOIN pg_namespace n ON n.oid = p.pronamespace,
       LATERAL aclexplode(p.proacl) a
 WHERE pg_get_userbyid(a.grantee) = ANY($1::text[])
"""

_SQL_DEFAULT_ACL = """
SELECT d.defaclobjtype::text AS object,
       a.privilege_type AS privilege,
       pg_get_userbyid(a.grantee) AS role
  FROM pg_default_acl d, LATERAL aclexplode(d.defaclacl) a
 WHERE pg_get_userbyid(a.grantee) = ANY($1::text[])
"""

_SQL_OWNERSHIP = """
SELECT n.nspname || '.' || c.relname AS object, 'relation' AS detail,
       pg_get_userbyid(c.relowner) AS role
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE pg_get_userbyid(c.relowner) = ANY($1::text[])
UNION ALL
SELECT n.nspname AS object, 'schema' AS detail, pg_get_userbyid(n.nspowner) AS role
  FROM pg_namespace n
 WHERE pg_get_userbyid(n.nspowner) = ANY($1::text[])
"""

# 成员关系两个方向：``roleid`` 是「被授予的角色」、``member`` 是「持有者」。
_SQL_MEMBERSHIP = """
SELECT pg_get_userbyid(m.roleid) AS role, pg_get_userbyid(m.member) AS object,
       'granted_to' AS detail
  FROM pg_auth_members m
 WHERE pg_get_userbyid(m.roleid) = ANY($1::text[])
UNION ALL
SELECT pg_get_userbyid(m.member) AS role, pg_get_userbyid(m.roleid) AS object,
       'member_of' AS detail
  FROM pg_auth_members m
 WHERE pg_get_userbyid(m.member) = ANY($1::text[])
"""

# 权威：``DROP ROLE`` 实际检查的依赖表，按数据库汇总。
_SQL_SHADOW = """
SELECT (SELECT datname FROM pg_database WHERE oid = d.dbid) AS database,
       count(*)::int AS n
  FROM pg_shdepend d
  JOIN pg_roles r ON r.oid = d.refobjid
 WHERE d.refclassid = 'pg_authid'::regclass
   AND r.rolname = ANY($1::text[])
 GROUP BY 1
"""

_REFERENCE_QUERIES: tuple[tuple[str, str], ...] = (
    ("policy_roles", _SQL_POLICY_ROLES),
    ("policy_expr", _SQL_POLICY_EXPR),
    ("table_acl", _SQL_CLASS_ACL),
    ("schema_acl", _SQL_NAMESPACE_ACL),
    ("function_acl", _SQL_PROC_ACL),
    ("default_acl", _SQL_DEFAULT_ACL),
    ("ownership", _SQL_OWNERSHIP),
    ("membership", _SQL_MEMBERSHIP),
)


async def _references_in(database: str, conn: asyncpg.Connection) -> list[Reference]:
    roles = list(LEGACY_ROLES)
    found: list[Reference] = []
    for kind, sql in _REFERENCE_QUERIES:
        for row in await conn.fetch(sql, roles):
            detail = {key: value for key, value in dict(row).items() if key != "role"}
            found.append(
                Reference(
                    database=database,
                    role=str(row["role"]),
                    kind=kind,
                    detail=json.dumps(detail, default=str, ensure_ascii=False),
                )
            )
    return found


async def _survey() -> Assessment:
    roles = list(LEGACY_ROLES)

    maintenance = await asyncpg.connect(**_dsn(_MAINTENANCE_DATABASE))
    try:
        databases = [row["datname"] for row in await maintenance.fetch(_SQL_DATABASES)]
        existing = tuple(row["rolname"] for row in await maintenance.fetch(_SQL_EXISTING, roles))
        shadow = [
            (str(row["database"]), int(row["n"]))
            for row in await maintenance.fetch(_SQL_SHADOW, roles)
        ]
    finally:
        await maintenance.close()

    references: list[Reference] = []
    for database in (name for name in databases if name not in _EXCLUDED_DATABASES):
        try:
            conn = await asyncpg.connect(**_dsn(database))
        except (asyncpg.PostgresError, OSError) as exc:  # 连不上就明说，不静默跳过
            references.append(
                Reference(
                    database=database,
                    role=",".join(LEGACY_ROLES),
                    kind="unreachable",
                    detail=str(exc),
                )
            )
            continue
        try:
            references.extend(await _references_in(database, conn))
        finally:
            await conn.close()

    return assess(
        existing=existing,
        references=tuple(references),
        shadow_dependencies=tuple(shadow),
    )


async def _drop() -> list[dict[str, str]]:
    actions: list[dict[str, str]] = []
    conn = await asyncpg.connect(**_dsn(_MAINTENANCE_DATABASE))
    try:
        for role in LEGACY_ROLES:
            if not await conn.fetchval("SELECT 1 FROM pg_roles WHERE rolname = $1", role):
                actions.append({"role": role, "action": "absent"})
                continue
            await conn.execute(f'DROP ROLE "{role}"')
            actions.append({"role": role, "action": "dropped"})
    finally:
        await conn.close()
    return actions


def _report(result: Assessment) -> dict[str, object]:
    return {
        "existing": list(result.existing),
        "deletable": result.deletable,
        "shadow_dependencies": [
            {"database": database, "count": count}
            for database, count in result.shadow_dependencies
        ],
        "references": [asdict(item) for item in result.references],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="退役遗留壳角色（默认只检查）")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--check", action="store_true", help="只报告，不执行 DROP")
    group.add_argument("--drop", action="store_true", help="零引用时才执行 DROP")
    args = parser.parse_args(argv)

    result = asyncio.run(_survey())
    print(json.dumps(_report(result), ensure_ascii=False, indent=2))

    if not args.drop:
        return EXIT_CLEAN if result.deletable or not result.existing else EXIT_BLOCKED
    if result.shadow_dependencies or result.references:
        print(
            "拒绝执行 DROP：先清掉上面列出的引用（它们分布在不同数据库里，"
            "需要按库逐个处理），本工具不代做授权清理。",
            file=sys.stderr,
        )
        return EXIT_BLOCKED
    try:
        for action in asyncio.run(_drop()):
            print(json.dumps(action))
    except asyncpg.PostgresError as exc:
        print(f"DROP ROLE 失败：{exc}", file=sys.stderr)
        return EXIT_DROP_FAILED
    return EXIT_CLEAN


if __name__ == "__main__":
    raise SystemExit(main())
