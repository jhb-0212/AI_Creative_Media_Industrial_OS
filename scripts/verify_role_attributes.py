"""角色属性漂移闸门：声明（``packages.shared.role_specs``）vs 活集群。

为什么要有它
------------
AUTHZ-7（迁移执行者仍是超级用户）暂时关不掉：角色创建与授权 DDL 散在二十多个
历史迁移里，抽出来等于重建时间胶囊。债务没清之前，至少要让「声明」与「活库」
不能悄悄分叉——否则等某天有人 ``ALTER ROLE svc_asset SUPERUSER``，或者把
``qc_maintenance_login`` 的 INHERIT 关掉，唯一的发现途径会是某条不相干的
集成用例偶发失败。

判据（全部只读）
----------------
1. 声明的角色必须都存在；
2. 任何角色不得 ``SUPERUSER / CREATEDB / CREATEROLE / BYPASSRLS``；
3. 可登录性必须与声明一致（维护 owner/operator 不可登录，其余可登录）；
4. ``INHERIT`` 不得关闭（关了成员关系不传递，``login`` 拿不到 operator，
   现象是「命令被拒」而不是「配置错」）。

``--include-undeclared`` 额外列出「集群里有、声明里没有」的角色——这是盘点而
不是闸门判据：运维手工建的身份不该让 CI 变红，但值得每次部署看一眼。

用法::

    python scripts/verify_role_attributes.py                 # 漂移则 exit 1
    python scripts/verify_role_attributes.py --include-undeclared

退出码：0=一致 / 1=漂移 / 2=连不上库（环境阻塞，不记为代码缺陷）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

import asyncpg  # type: ignore[import-untyped]  # 无 py.typed；只用 connect/fetch

from packages.shared.config import get_settings
from packages.shared.role_specs import LiveRole, diff_role_attributes, role_specs

EXIT_CLEAN = 0
EXIT_DRIFT = 1
EXIT_BLOCKED = 2

# 系统角色（``pg_*`` / ``postgres``）不属于本仓库声明的身份面。
_SYSTEM_PREFIXES = ("pg_",)
_SYSTEM_ROLES = frozenset({"postgres"})


async def _live_roles() -> tuple[LiveRole, ...]:
    settings = get_settings()
    conn = await asyncpg.connect(
        host=settings.postgres_host,
        port=settings.postgres_port,
        user=settings.postgres_user,
        password=settings.postgres_password.get_secret_value(),
        database="postgres",
    )
    try:
        rows = await conn.fetch(
            """
            SELECT rolname, rolcanlogin, rolsuper, rolcreatedb,
                   rolcreaterole, rolbypassrls, rolinherit
              FROM pg_roles
            """
        )
    finally:
        await conn.close()
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


def _is_system_role(name: str) -> bool:
    return name in _SYSTEM_ROLES or name.startswith(_SYSTEM_PREFIXES)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="角色属性漂移检查（只读）")
    parser.add_argument(
        "--include-undeclared",
        action="store_true",
        help="额外列出集群里存在但未声明的非系统角色（盘点，不影响退出码）",
    )
    args = parser.parse_args(argv)

    specs = role_specs()
    try:
        live = asyncio.run(_live_roles())
    except (asyncpg.PostgresError, OSError) as exc:
        print(f"[BLOCKED-ENV] 连不上库，无法取得证据：{exc}", file=sys.stderr)
        return EXIT_BLOCKED

    problems = diff_role_attributes(specs, live)
    declared = {spec.name for spec in specs}
    undeclared = sorted(
        role.name
        for role in live
        if role.name not in declared and not _is_system_role(role.name)
    )

    report: dict[str, object] = {
        "declared": len(specs),
        "checked": len([r for r in live if r.name in declared]),
        "problems": problems,
        "clean": not problems,
    }
    if args.include_undeclared:
        report["undeclared"] = undeclared
    print(json.dumps(report, ensure_ascii=False, indent=2))

    for problem in problems:
        print(f"[DRIFT] {problem}", file=sys.stderr)
    return EXIT_CLEAN if not problems else EXIT_DRIFT


if __name__ == "__main__":
    raise SystemExit(main())
