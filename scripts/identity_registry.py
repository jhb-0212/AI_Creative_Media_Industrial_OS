"""角色 Registry 对账：``role_specs.py``（唯一事实源）vs 活集群（只读）。

为什么不是「再写一份 YAML」
--------------------------
WP-01 的裁决是：角色定义只留在 ``packages/shared/role_specs.py``。本脚本只做两件
事——**导出生成物** 和 **与 catalog 对账**，绝不把导出的 JSON/YAML 反过来当输入。
一旦允许「改 JSON 再回灌」，就会回到两份事实源互相打架的老路（Python 定义属性、
YAML 定义生命周期、CI 只校验其中一份）。

判据（全部只读）
----------------
1. 声明自洽：``role_specs.spec_problems``（提权属性、NOLOGIN 配口令、生命周期取值）；
2. 集群一致：缺角色 / 提权属性 / 登录性 / INHERIT 漂移（复用 ``diff_role_attributes``）；
3. 成员关系：声明的 ``member_of`` 必须在 ``pg_auth_members`` 里真实存在；
4. 生命周期归属：``lifecycle_owner=migration`` 的角色不应出现在 bootstrap 声明集
   （当前声明集全部为 bootstrap，出现即说明声明被改坏）。

用法::

    python scripts/identity_registry.py --dump build/identity_registry.json
    python scripts/identity_registry.py --check              # 漂移则 exit 1
    python scripts/identity_registry.py --check --include-undeclared

退出码：0=一致 / 1=漂移 / 2=连不上库（环境阻塞，不记为代码缺陷）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import asyncpg  # type: ignore[import-untyped]

from packages.shared.config import get_settings
from packages.shared.role_specs import (
    LiveRole,
    diff_role_attributes,
    registry_payload,
    retired_registry_payload,
    retired_role_names,
    retired_role_problems,
    role_specs,
    spec_problems,
)

EXIT_CLEAN = 0
EXIT_DRIFT = 1
EXIT_BLOCKED = 2

_SYSTEM_PREFIXES = ("pg_",)
_SYSTEM_ROLES = frozenset({"postgres"})


async def _fetch_live() -> tuple[tuple[LiveRole, ...], list[tuple[str, str]]]:
    settings = get_settings()
    conn = await asyncpg.connect(
        host=settings.postgres_host,
        port=settings.postgres_port,
        user=settings.postgres_user,
        password=settings.postgres_password.get_secret_value(),
        database="postgres",
    )
    try:
        roles = await conn.fetch(
            """
            SELECT rolname, rolcanlogin, rolsuper, rolcreatedb,
                   rolcreaterole, rolbypassrls, rolinherit
              FROM pg_roles
            """
        )
        memberships = await conn.fetch(
            """
            SELECT m.rolname AS member, g.rolname AS granted
              FROM pg_auth_members a
              JOIN pg_roles m ON m.oid = a.member
              JOIN pg_roles g ON g.oid = a.roleid
            """
        )
    finally:
        await conn.close()
    live = tuple(
        LiveRole(
            name=str(row["rolname"]),
            login=bool(row["rolcanlogin"]),
            superuser=bool(row["rolsuper"]),
            create_db=bool(row["rolcreatedb"]),
            create_role=bool(row["rolcreaterole"]),
            bypass_rls=bool(row["rolbypassrls"]),
            inherit=bool(row["rolinherit"]),
        )
        for row in roles
    )
    pairs = [(str(row["member"]), str(row["granted"])) for row in memberships]
    return live, pairs


def _is_system_role(name: str) -> bool:
    return name in _SYSTEM_ROLES or name.startswith(_SYSTEM_PREFIXES)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="角色 Registry 导出 / 对账（只读）")
    parser.add_argument("--dump", metavar="PATH", help="把声明导出为 JSON（生成物）")
    parser.add_argument("--check", action="store_true", help="与活集群对账，漂移则 exit 1")
    parser.add_argument(
        "--include-undeclared",
        action="store_true",
        help="额外列出集群里存在但未声明的非系统角色（盘点，不影响退出码）",
    )
    args = parser.parse_args(argv)

    specs = role_specs()
    problems = spec_problems(specs)
    problems.extend(retired_role_problems())
    retired_names = retired_role_names()

    if args.dump:
        payload = registry_payload(specs)
        path = Path(args.dump)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "source": "packages/shared/role_specs.py",
                    "generated": True,
                    "warning": "生成物，不得作为第二事实源回灌",
                    "roles": payload,
                    "retired": retired_registry_payload(),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"[DUMP] {len(payload)} roles + {len(retired_registry_payload())} retired -> {path}")

    if not args.check:
        report = {
            "declared": len(specs),
            "spec_problems": problems,
            "clean": not problems,
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
        for problem in problems:
            print(f"[SPEC] {problem}", file=sys.stderr)
        return EXIT_CLEAN if not problems else EXIT_DRIFT

    try:
        live, memberships = asyncio.run(_fetch_live())
    except (asyncpg.PostgresError, OSError) as exc:
        print(f"[BLOCKED-ENV] 连不上库，无法取得证据：{exc}", file=sys.stderr)
        return EXIT_BLOCKED

    problems.extend(diff_role_attributes(specs, live))
    declared = {spec.name: spec for spec in specs}
    present = {role.name for role in live}
    for spec in specs:
        for group in spec.member_of:
            if (spec.name, group) not in memberships:
                problems.append(f"{spec.name}：声明的成员关系 {group} 在集群里不存在")
        if spec.lifecycle_owner == "migration":
            problems.append(f"{spec.name}：bootstrap 声明集里出现 lifecycle_owner=migration 的角色")

    undeclared = sorted(
        name for name in present if name not in declared and not _is_system_role(name)
    )
    present_retired = sorted(name for name in present if name in retired_names)
    for name in present_retired:
        problems.append(f"{name}：已退役身份仍存在于集群（退役未落实或已被重建）")
    report = {
        "declared": len(specs),
        "retired": len(retired_names),
        "checked": len([role for role in live if role.name in declared]),
        "memberships_checked": sum(len(spec.member_of) for spec in specs),
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
