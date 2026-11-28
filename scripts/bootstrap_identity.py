"""身份引导执行器：``roles``（迁移前）/ ``grants``（迁移后）/ ``verify``。

用法::

    python scripts/bootstrap_identity.py --dry-run            # 打印全部语句，不连库
    python scripts/bootstrap_identity.py --roles              # 迁移前：建角色、收敛属性
    python scripts/bootstrap_identity.py --grants             # 迁移后：收敛授权面
    python scripts/bootstrap_identity.py --all                # 依次执行（要求已到 head）
    python scripts/bootstrap_identity.py --verify             # 只读：声明 vs 活库

为什么执行器必须用超管
----------------------
``ALTER ROLE ... NOSUPERUSER`` 只有超管能跑（实测见
``tests/integration/test_identity_bootstrap_boundary_pg.py``）。这不是「权限给
得不够」，而是 PG 把 ``SUPERUSER`` 属性设为超管专属。bootstrap 因此就是那个
**有界特权窗口**：只跑声明里的语句、只碰身份面、跑完即退。迁移执行者则相反，
必须是非超管——两者职责分开，各自的权限才都能压到最小。

退出码：0=成功 / 1=执行失败或漂移 / 2=环境阻塞（连不上、schema 未到 head）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

import asyncpg  # type: ignore[import-untyped]

from packages.shared.config import get_settings
from packages.shared.identity_bootstrap import (
    Phase,
    grants_phase,
    missing_objects,
    roles_phase,
)
from packages.shared.role_specs import (
    LiveRole,
    diff_role_attributes,
    retired_role_names,
    role_specs,
    spec_problems,
)

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_BLOCKED = 2


def _dsn(settings: object, database: str) -> str:
    password = settings.postgres_password.get_secret_value()
    return (
        f"postgresql://{settings.postgres_user}:{password}@"
        f"{settings.postgres_host}:{settings.postgres_port}/{database}"
    )


async def _connect(settings: object) -> asyncpg.Connection:
    return await asyncpg.connect(_dsn(settings, settings.postgres_db))


async def _apply(phase: Phase, conn: asyncpg.Connection) -> int:
    for index, statement in enumerate(phase.statements, start=1):
        try:
            await conn.execute(statement)
        except asyncpg.PostgresError as exc:
            print(
                f"[FAILED] {phase.name} 第 {index}/{len(phase.statements)} 条失败：{exc}\n"
                f"  {statement}",
                file=sys.stderr,
            )
            return EXIT_FAILED
    print(f"[OK] {phase.name}：{len(phase.statements)} 条语句执行完毕")
    return EXIT_OK


async def _existing_tables(conn: asyncpg.Connection) -> dict[str, frozenset[str]]:
    rows = await conn.fetch(
        """
        SELECT n.nspname AS schema, c.relname AS table
          FROM pg_class c
          JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE c.relkind IN ('r', 'p', 'v', 'm')
        """
    )
    grouped: dict[str, set[str]] = {}
    for row in rows:
        grouped.setdefault(str(row["schema"]), set()).add(str(row["table"]))
    return {schema: frozenset(tables) for schema, tables in grouped.items()}


async def _live_roles(conn: asyncpg.Connection) -> tuple[LiveRole, ...]:
    rows = await conn.fetch(
        """
        SELECT rolname, rolcanlogin, rolsuper, rolcreatedb,
               rolcreaterole, rolbypassrls, rolinherit
          FROM pg_roles
        """
    )
    return tuple(
        LiveRole(
            name=str(row["rolname"]),
            login=bool(row["rolcanlogin"]),
            superuser=bool(row["rolsuper"]),
            create_db=bool(row["rolcreatedb"]),
            create_role=bool(row["rolcreaterole"]),
            bypass_rls=bool(row["rolbypassrls"]),
            inherit=bool(row["rolinherit"]),
        )
        for row in rows
    )


async def _run(args: argparse.Namespace) -> int:
    settings = get_settings()

    if args.dry_run:
        phases = [
            roles_phase(settings),
            grants_phase(server_version_num=args.server_version or 160000),
        ]
        for phase in phases:
            print(f"-- {phase.name}: {len(phase.statements)} statements")
            for statement in phase.statements:
                print(statement)
        return EXIT_OK

    conn = await _connect(settings)
    try:
        if args.verify:
            declared = role_specs()
            problems = diff_role_attributes(declared, await _live_roles(conn))
            print(
                json.dumps(
                    {"declared": len(declared), "problems": problems, "clean": not problems},
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return EXIT_OK if not problems else EXIT_FAILED

        if args.roles or args.all:
            # 退役身份只能由受控降级入口恢复，bootstrap 永不创建：
            # `RETIRED_ROLES` 与 `role_specs()` 一旦重叠，就是有人把退役身份
            # 放回了"当前目标态"，此时建角色等于让退役失效——直接阻塞。
            overlap = sorted(
                spec.name for spec in role_specs() if spec.name in retired_role_names()
            )
            if overlap:
                print(
                    "[BLOCKED] 退役身份出现在当前目标态：\n  " + "\n  ".join(overlap),
                    file=sys.stderr,
                )
                return EXIT_BLOCKED
            problems = spec_problems()
            if problems:
                print(
                    "[BLOCKED] 声明自检未通过：\n  " + "\n  ".join(problems),
                    file=sys.stderr,
                )
                return EXIT_BLOCKED
            code = await _apply(roles_phase(settings), conn)
            if code != EXIT_OK:
                return code

        if args.grants or args.all:
            missing = missing_objects(await _existing_tables(conn))
            if missing:
                print(
                    "[BLOCKED] schema 未到 head，缺少对象：\n  " + "\n  ".join(missing),
                    file=sys.stderr,
                )
                return EXIT_BLOCKED
            version = await conn.fetchval("SELECT current_setting('server_version_num')::int")
            code = await _apply(grants_phase(server_version_num=int(version)), conn)
            if code != EXIT_OK:
                return code
        return EXIT_OK
    finally:
        await conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="身份引导（bootstrap）")
    parser.add_argument("--roles", action="store_true", help="迁移前：建角色与收敛属性")
    parser.add_argument("--grants", action="store_true", help="迁移后：收敛授权面")
    parser.add_argument("--all", action="store_true", help="roles 后接 grants")
    parser.add_argument("--verify", action="store_true", help="只读：声明 vs 活库")
    parser.add_argument("--dry-run", action="store_true", help="只打印语句，不连库")
    parser.add_argument(
        "--server-version",
        type=int,
        default=None,
        help="--dry-run 时假定的 server_version_num（默认 160000）",
    )
    args = parser.parse_args(argv)
    if not any((args.roles, args.grants, args.all, args.verify, args.dry_run)):
        parser.error("至少选一个动作：--roles / --grants / --all / --verify / --dry-run")
    if args.verify and (args.roles or args.grants or args.all):
        parser.error("--verify 是只读动作，不能与写入动作混用")

    try:
        return asyncio.run(_run(args))
    except (asyncpg.PostgresError, OSError) as exc:
        print(f"[BLOCKED-ENV] 连不上库：{exc}", file=sys.stderr)
        return EXIT_BLOCKED


if __name__ == "__main__":
    raise SystemExit(main())
