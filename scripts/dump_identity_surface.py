"""身份面快照与比对：把「集群身份长什么样」变成可 diff 的 JSON。

为什么需要它
------------
AUTHZ-7 的解除路径要求「把角色 DDL 从迁移里搬走」，而搬动 42 个迁移、238 处动作
的**前提**是能证明「搬完之后集群没变样」。靠人看 `pg_roles` 不行：真正的身份面
还包括成员关系、schema/表/列/函数的 ACL、默认权限、对象属主、以及每个策略里
引用到的角色——少看一层，「搬」就可能悄悄放宽某条权限。

因此本脚本把七层一次性拍平成有序 JSON，并提供 `--compare`：
同两份快照逐条比，差异按「层 + 对象」报出来，而不是一句"不一致"。

口径（刻意排除的东西）
----------------------
* 不比 ``grantor``：一次 GRANT 记住的是执行者的身份，而执行者在重建前后可能由
  bootstrap 或迁移发起，这属于**过程**，不是身份面的目标态。
* 不比 ``rolpassword``：口令哈希带随机盐，永远不相等；口令是否存在由
  ``identity_surface`` 的 ``password_present`` 布尔表达（有/无即可）。
* 不比 ``pg_authid`` 里系统角色（``pg_*``）与 ``postgres`` 自身。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

import asyncpg  # type: ignore[import-untyped]

from packages.shared.config import get_settings

EXIT_EQUAL = 0
EXIT_DIFFERENT = 1
EXIT_BLOCKED = 2

_REPO_ROOT = Path(__file__).resolve().parents[1]
# 「回放等价」的存档：迁移链重建后必须仍然等于它，否则就是放宽或收紧过身份面。
_BASELINE_PATH = _REPO_ROOT / "data" / "migrations" / "identity_surface_baseline.json"

SECTIONS = (
    "roles",
    "memberships",
    "ownership",
    "schema_acl",
    "relation_acl",
    "column_acl",
    "default_acl",
    "function_acl",
    "policies",
)


def _or_public(role: str | None) -> str:
    # aclexplode 对 PUBLIC 返回 0（不是 NULL）；这里统一成字符串，避免 0/None
    # 两种空在 JSON 里表现不同导致假差异。
    return role if role else "PUBLIC"


async def _roles(conn: asyncpg.Connection) -> list[list[Any]]:
    rows = await conn.fetch(
        """
        SELECT a.rolname, a.rolcanlogin, a.rolsuper, a.rolcreatedb,
               a.rolcreaterole, a.rolbypassrls, a.rolinherit,
               (a.rolpassword IS NOT NULL) AS password_present
          FROM pg_authid a
         WHERE a.rolname NOT LIKE 'pg\\_%' AND a.rolname <> 'postgres'
        """
    )
    return [
        [
            row["rolname"], row["rolcanlogin"], row["rolsuper"], row["rolcreatedb"],
            row["rolcreaterole"], row["rolbypassrls"], row["rolinherit"],
            row["password_present"],
        ]
        for row in rows
    ]


async def _memberships(conn: asyncpg.Connection) -> list[list[Any]]:
    rows = await conn.fetch(
        """
        SELECT r.rolname AS granted, m.rolname AS member,
               a.admin_option, a.inherit_option, a.set_option
          FROM pg_auth_members a
          JOIN pg_roles r ON r.oid = a.roleid
          JOIN pg_roles m ON m.oid = a.member
         WHERE r.rolname NOT LIKE 'pg\\_%'
        """
    )
    return [
        [row["granted"], row["member"], row["admin_option"], row["inherit_option"],
         row["set_option"]]
        for row in rows
    ]


async def _ownership(conn: asyncpg.Connection) -> list[list[Any]]:
    rows = await conn.fetch(
        """
        SELECT 'schema' AS kind, n.nspname AS object, r.rolname AS owner
          FROM pg_namespace n JOIN pg_roles r ON r.oid = n.nspowner
         WHERE n.nspname NOT LIKE 'pg\\_%' AND n.nspname <> 'information_schema'
        UNION ALL
        SELECT 'relation', n.nspname || '.' || c.relname, r.rolname
          FROM pg_class c
          JOIN pg_namespace n ON n.oid = c.relnamespace
          JOIN pg_roles r ON r.oid = c.relowner
         WHERE n.nspname NOT LIKE 'pg\\_%'
        UNION ALL
        SELECT 'function', n.nspname || '.' || p.proname, r.rolname
          FROM pg_proc p
          JOIN pg_namespace n ON n.oid = p.pronamespace
          JOIN pg_roles r ON r.oid = p.proowner
         WHERE n.nspname NOT LIKE 'pg\\_%'
        """
    )
    return [[row["kind"], row["object"], row["owner"]] for row in rows]


def _acl_rows(rows: Iterable[asyncpg.Record], kind_col: str = "object") -> list[list[Any]]:
    return [
        [row[kind_col], row["privilege"], row["grantee"], row["is_grantable"]]
        for row in rows
    ]


async def _schema_acl(conn: asyncpg.Connection) -> list[list[Any]]:
    rows = await conn.fetch(
        """
        SELECT n.nspname AS object, acl.privilege_type AS privilege,
               COALESCE(r.rolname, 'PUBLIC') AS grantee, acl.is_grantable
          FROM pg_namespace n, aclexplode(n.nspacl) acl
          LEFT JOIN pg_roles r ON r.oid = acl.grantee
         WHERE n.nspname NOT LIKE 'pg\\_%' AND n.nspname <> 'information_schema'
        """
    )
    return _acl_rows(rows)


async def _relation_acl(conn: asyncpg.Connection) -> list[list[Any]]:
    rows = await conn.fetch(
        """
        SELECT n.nspname || '.' || c.relname AS object,
               acl.privilege_type AS privilege,
               COALESCE(r.rolname, 'PUBLIC') AS grantee, acl.is_grantable
          FROM pg_class c
          JOIN pg_namespace n ON n.oid = c.relnamespace, aclexplode(c.relacl) acl
          LEFT JOIN pg_roles r ON r.oid = acl.grantee
         WHERE n.nspname NOT LIKE 'pg\\_%'
        """
    )
    return _acl_rows(rows)


async def _column_acl(conn: asyncpg.Connection) -> list[list[Any]]:
    rows = await conn.fetch(
        """
        SELECT n.nspname || '.' || c.relname || '.' || att.attname AS object,
               acl.privilege_type AS privilege,
               COALESCE(r.rolname, 'PUBLIC') AS grantee, acl.is_grantable
          FROM pg_attribute att
          JOIN pg_class c ON c.oid = att.attrelid
          JOIN pg_namespace n ON n.oid = c.relnamespace,
          aclexplode(att.attacl) acl
          LEFT JOIN pg_roles r ON r.oid = acl.grantee
         WHERE n.nspname NOT LIKE 'pg\\_%' AND att.attacl IS NOT NULL
        """
    )
    return _acl_rows(rows)


async def _default_acl(conn: asyncpg.Connection) -> list[list[Any]]:
    rows = await conn.fetch(
        """
        SELECT COALESCE(n.nspname, '*') AS schema_name, d.defaclobjtype,
               COALESCE(r.rolname, 'PUBLIC') AS owner,
               acl.privilege_type AS privilege,
               COALESCE(g.rolname, 'PUBLIC') AS grantee
          FROM pg_default_acl d
          LEFT JOIN pg_namespace n ON n.oid = d.defaclnamespace
          JOIN pg_roles r ON r.oid = d.defaclrole,
          aclexplode(d.defaclacl) acl
          LEFT JOIN pg_roles g ON g.oid = acl.grantee
        """
    )
    return [
        [row["schema_name"], row["defaclobjtype"], row["owner"], row["privilege"],
         row["grantee"]]
        for row in rows
    ]


async def _function_acl(conn: asyncpg.Connection) -> list[list[Any]]:
    rows = await conn.fetch(
        """
        SELECT n.nspname || '.' || p.proname AS object,
               acl.privilege_type AS privilege,
               COALESCE(r.rolname, 'PUBLIC') AS grantee, acl.is_grantable
          FROM pg_proc p
          JOIN pg_namespace n ON n.oid = p.pronamespace, aclexplode(p.proacl) acl
          LEFT JOIN pg_roles r ON r.oid = acl.grantee
         WHERE n.nspname NOT LIKE 'pg\\_%'
        """
    )
    return _acl_rows(rows)


async def _policies(conn: asyncpg.Connection) -> list[list[Any]]:
    rows = await conn.fetch(
        """
        SELECT schemaname, tablename, policyname, permissive, roles::text AS roles,
               cmd, COALESCE(qual, '') AS qual, COALESCE(with_check, '') AS with_check
          FROM pg_policies
         WHERE schemaname NOT LIKE 'pg\\_%'
        """
    )
    return [
        [row["schemaname"], row["tablename"], row["policyname"], row["permissive"],
         row["roles"], row["cmd"], row["qual"], row["with_check"]]
        for row in rows
    ]


async def dump(conn: asyncpg.Connection) -> dict[str, list[list[Any]]]:
    """拍平整个身份面；每层按字符串排序，保证两次快照可直接逐条比对。"""
    surface: dict[str, list[list[Any]]] = {
        "roles": await _roles(conn),
        "memberships": await _memberships(conn),
        "ownership": await _ownership(conn),
        "schema_acl": await _schema_acl(conn),
        "relation_acl": await _relation_acl(conn),
        "column_acl": await _column_acl(conn),
        "default_acl": await _default_acl(conn),
        "function_acl": await _function_acl(conn),
        "policies": await _policies(conn),
    }
    for rows in surface.values():
        rows.sort(key=lambda row: json.dumps(row, ensure_ascii=False, default=str))
    return surface


def diff(left: dict[str, list[list[Any]]], right: dict[str, list[list[Any]]]) -> list[str]:
    """逐层逐条比，返回人类可读差异（空 = 一致）。"""
    problems: list[str] = []
    for section in SECTIONS:
        left_rows = left.get(section, [])
        right_rows = right.get(section, [])
        left_set = {json.dumps(row, ensure_ascii=False, default=str) for row in left_rows}
        right_set = {json.dumps(row, ensure_ascii=False, default=str) for row in right_rows}
        for row in sorted(left_set - right_set):
            problems.append(f"{section} 仅存在于左：{row}")
        for row in sorted(right_set - left_set):
            problems.append(f"{section} 仅存在于右：{row}")
    return problems


async def _dump_from_settings() -> dict[str, list[list[Any]]]:
    settings = get_settings()
    conn = await asyncpg.connect(
        host=settings.postgres_host,
        port=settings.postgres_port,
        user=settings.postgres_user,
        password=settings.postgres_password.get_secret_value(),
        database=settings.postgres_db,
    )
    try:
        return await dump(conn)
    finally:
        await conn.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="身份面快照 / 比对")
    parser.add_argument("--dump", metavar="PATH", help="把快照写入 JSON 文件")
    parser.add_argument(
        "--compare", nargs=2, metavar=("LEFT", "RIGHT"),
        help="比较两份快照 JSON（LEFT 减 RIGHT 与 RIGHT 减 LEFT 都报）",
    )
    parser.add_argument("--summary", action="store_true", help="只打印各层条数")
    parser.add_argument(
        "--check-baseline",
        action="store_true",
        help="把活库快照与 data/migrations/identity_surface_baseline.json 比对",
    )
    args = parser.parse_args(argv)

    if args.compare:
        left = json.loads(Path(args.compare[0]).read_text(encoding="utf-8"))
        right = json.loads(Path(args.compare[1]).read_text(encoding="utf-8"))
        problems = diff(left, right)
        for problem in problems:
            print(problem, file=sys.stderr)
        print(f"[{'EQUAL' if not problems else 'DIFFERENT'}] 差异 {len(problems)} 条")
        return EXIT_EQUAL if not problems else EXIT_DIFFERENT

    try:
        surface = asyncio.run(_dump_from_settings())
    except (asyncpg.PostgresError, OSError) as exc:
        print(f"[BLOCKED-ENV] 连不上库：{exc}", file=sys.stderr)
        return EXIT_BLOCKED

    if args.summary or not (args.dump or args.check_baseline):
        counts = {key: len(surface[key]) for key in SECTIONS}
        print(json.dumps(counts, ensure_ascii=False, indent=2))
    if args.dump:
        Path(args.dump).write_text(
            json.dumps(surface, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
        print(f"[DUMP] {args.dump}")

    if args.check_baseline:
        if not _BASELINE_PATH.exists():
            print(f"[BLOCKED] 基线不存在：{_BASELINE_PATH}", file=sys.stderr)
            return EXIT_BLOCKED
        problems = diff(
            json.loads(_BASELINE_PATH.read_text(encoding="utf-8")), surface
        )
        for problem in problems[:50]:
            print(problem, file=sys.stderr)
        print(f"[{'PASS' if not problems else 'DRIFT'}] 与基线差异 {len(problems)} 条")
        if problems:
            return EXIT_DIFFERENT
    return EXIT_EQUAL


if __name__ == "__main__":
    raise SystemExit(main())
