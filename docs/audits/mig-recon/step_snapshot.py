"""逐 revision 步进快照：在**全新集群**上重放，并在每个冲突 Revision 之后立即取样。

为什么必须用全新集群而不是复用已有实例：
1. 角色是集群级对象。在已跑过一遍的实例上重放，``CREATE ROLE`` 会变
   ``ALTER ROLE``，观测不到「首次创建」与「已存在时收敛」这两个分支的差异——
   而 T3 的差异恰好就落在这两个分支上。
2. 复用实例还要 DROP DATABASE，会污染已完成的 head 快照实验。

快照只记录「可比对的结构事实」，不做全库 dump（全库 dump 已由 head 快照负责）：
schema 集合、表与列数、角色属性、表级授权摘要、以及冲突相关对象的存在性。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

# 必须用 Windows 盘符路径：git-bash 的 /d/... 映射在 subprocess 里不可见。
PSQL = r"D:\Program Files\PostgreSQL\16\bin\psql.exe"

# 10 个分叉 revision（与冻结清单一致）
CONFLICT_REVS = {
    "b3c4d5e6f7a8",
    "9e3b4c5d6e7f",
    "a1b2c3d4e5f6",
    "b5c6d7e8f9a0",
    "b7c8d9e0f1a2",
    "c1d2e3f4a5b6",
    "c6d7e8f9a0b1",
    "d4e5f6a7b8c9",
    "e5f6a7b8c9d0",
    "f6a7b8c9d1e2",
}

REV_RE = re.compile(r"^revision(?::\s*[^=]+)?\s*=\s*[\"']([^\"']+)", re.M)
DOWN_RE = re.compile(r"^down_revision(?::\s*[^=]+)?\s*=\s*(.+)$", re.M)


def read_chain(worktree: Path) -> list[str]:
    """从迁移文件解析出 base → head 的线性链。"""
    downs: dict[str, str | None] = {}
    for path in (worktree / "data/migrations/versions").glob("*.py"):
        source = path.read_text(encoding="utf-8")
        rev = REV_RE.search(source)
        if not rev:
            continue
        down_match = DOWN_RE.search(source)
        down: str | None = None
        if down_match:
            literal = re.search(r"[\"']([^\"']+)[\"']", down_match.group(1))
            down = literal.group(1) if literal else None
        downs[rev.group(1)] = down

    children = {v: k for k, v in downs.items() if v}
    roots = [k for k, v in downs.items() if v is None]
    if len(roots) != 1:
        raise SystemExit(f"{worktree}: 期望单根，实际 {roots}")
    chain, cursor = [], roots[0]
    while cursor:
        chain.append(cursor)
        cursor = children.get(cursor)
    if len(chain) != len(downs):
        raise SystemExit(f"{worktree}: 链长 {len(chain)} != 文件数 {len(downs)}（存在分叉或孤儿）")
    return chain


def q(port: str, sql: str, database: str = "ai_film_industrial") -> list[list[str]]:
    env = dict(os.environ, PGPASSWORD="<ROTATED-2026-11-28>")
    out = subprocess.run(
        [
            PSQL,
            "-h",
            "localhost",
            "-p",
            port,
            "-U",
            "postgres",
            "-X",
            "-A",
            "-F",
            "|",
            "-t",
            "-d",
            database,
            "-c",
            sql,
        ],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    ).stdout
    return [line.split("|") for line in out.splitlines() if line]


def snapshot(port: str) -> dict[str, object]:
    schemas = [
        r[0]
        for r in q(
            port,
            """
        SELECT nspname FROM pg_namespace
        WHERE nspname NOT LIKE 'pg_%' AND nspname <> 'information_schema' ORDER BY 1;""",
        )
    ]
    tables = {
        f"{r[0]}.{r[1]}": int(r[2])
        for r in q(
            port,
            """
        SELECT n.nspname, c.relname, count(a.attname)
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        LEFT JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum>0 AND NOT a.attisdropped
        WHERE c.relkind IN ('r','p') AND n.nspname NOT IN ('pg_catalog','information_schema')
        GROUP BY 1,2 ORDER BY 1,2;""",
        )
    }
    constraints = sorted(
        f"{r[0]}.{r[1]}.{r[2]}"
        for r in q(
            port,
            """
        SELECT n.nspname, c.relname, con.conname
        FROM pg_constraint con JOIN pg_class c ON c.oid=con.conrelid
        JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname NOT IN ('pg_catalog','information_schema') ORDER BY 1,2,3;""",
        )
    )
    roles = {
        r[0]: {
            "login": r[1],
            "super": r[2],
            "bypassrls": r[3],
            "createdb": r[4],
            "createrole": r[5],
        }
        for r in q(
            port,
            """
        SELECT rolname, rolcanlogin, rolsuper, rolbypassrls, rolcreatedb, rolcreaterole
        FROM pg_roles WHERE rolname NOT LIKE 'pg\\_%' ORDER BY 1;""",
            database="postgres",
        )
    }
    grants = {
        r[0]: {"objs": int(r[1]), "privs": r[2]}
        for r in q(
            port,
            """
        SELECT pg_get_userbyid(a.grantee), count(*),
               string_agg(DISTINCT a.privilege_type, '+' ORDER BY a.privilege_type)
        FROM pg_class c CROSS JOIN LATERAL aclexplode(c.relacl) a
        JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname NOT IN ('pg_catalog','information_schema')
          AND a.grantee <> c.relowner AND a.grantee <> 0
        GROUP BY 1 ORDER BY 1;""",
        )
    }
    memberships = sorted(
        f"{r[0]}->{r[1]}"
        for r in q(
            port,
            """
        SELECT parent.rolname, member.rolname FROM pg_auth_members am
        JOIN pg_roles parent ON parent.oid=am.roleid
        JOIN pg_roles member ON member.oid=am.member ORDER BY 1,2;""",
            database="postgres",
        )
    )
    return {
        "schemas": schemas,
        "table_count": len(tables),
        "tables": tables,
        "constraints": constraints,
        "roles": roles,
        "grants": grants,
        "memberships": memberships,
    }


def main() -> None:
    side = sys.argv[1]  # main | feat
    port = sys.argv[2]  # 15443 | 15444
    worktree = Path(sys.argv[3])
    outdir = Path(sys.argv[4])
    outdir.mkdir(parents=True, exist_ok=True)

    chain = read_chain(worktree)
    print(f"{side}: 链长 {len(chain)}，冲突点 {sorted(set(chain) & CONFLICT_REVS)}")

    env = dict(
        os.environ,
        POSTGRES_HOST="localhost",
        POSTGRES_PORT=port,
        POSTGRES_DB="ai_film_industrial",
        POSTGRES_USER="postgres",
        POSTGRES_PASSWORD="<ROTATED-2026-11-28>",
        POSTGRES_APP_PASSWORD="replay_app_pw",
        POSTGRES_RELAY_PASSWORD="replay_relay_pw",
        POSTGRES_PROJECT_PLANNING_PASSWORD="replay_pp_pw",
        POSTGRES_ASSET_SERVICE_PASSWORD="replay_as_pw",
        APP_ENV="development",
        PYTHONPATH=str(worktree),
    )

    def upgrade(target: str) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "-c", "alembic.ini", "upgrade", target],
            cwd=worktree,
            env=env,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise SystemExit(
                f"upgrade {target} 失败:\n{result.stdout[-2000:]}\n{result.stderr[-3000:]}"
            )

    for index, rev in enumerate(chain, 1):
        if rev not in CONFLICT_REVS:
            continue
        upgrade(rev)
        state = snapshot(port)
        state["_revision"] = rev
        state["_step"] = index
        path = outdir / f"{index:02d}-{rev}.json"
        path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
        print(
            f"  [{index:02d}] {rev} 表 {state['table_count']} 角色 {len(state['roles'])} "
            f"授权对象数 {sum(g['objs'] for g in state['grants'].values())}"
        )

    upgrade("head")
    head = snapshot(port)
    head["_revision"] = chain[-1]
    (outdir / "zz-head.json").write_text(
        json.dumps(head, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"  head {chain[-1]}: 表 {head['table_count']} 角色 {len(head['roles'])}")


if __name__ == "__main__":
    main()
