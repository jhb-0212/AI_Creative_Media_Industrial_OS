"""bootstrap 的真实集群判据：跑两遍不改结果，且与声明一致。

为什么必须跑真库
----------------
语句生成正确不代表能执行：``DO $$`` 里的 ``CREATE ROLE``、``ALTER ROLE`` 的属性
串、``converge_statements`` 里对表/函数的引用，任何一处拼错都只在真库上暴露
（``relation does not exist`` / ``syntax error at or near $$``）。

判据三条：
1. **幂等**：连跑两遍，第二遍不报错，且事后声明与集群逐项一致——bootstrap 会被
   每次部署重复调用，不幂等就意味着「部署次数决定集群状态」。
2. **授权面可收敛**：grants 阶段在 head 库上连跑两遍不报错（``converge_statements``
   是先 REVOKE 再 GRANT，重复执行结果相同）。
3. **preflight 有效**：head 库上 ``missing_objects`` 必须为空；不为空说明对象声明
   清单与迁移链已经分叉，此时执行 grants 只会得到一串莫名其妙的报错。
"""

from __future__ import annotations

import asyncpg
import pytest

from packages.shared.config import get_settings
from packages.shared.identity_bootstrap import (
    grants_phase,
    missing_objects,
    roles_phase,
)
from packages.shared.role_specs import LiveRole, diff_role_attributes, role_specs

pytestmark = pytest.mark.integration


async def _conn() -> asyncpg.Connection:
    settings = get_settings()
    return await asyncpg.connect(
        host=settings.postgres_host,
        port=settings.postgres_port,
        user=settings.postgres_user,
        password=settings.postgres_password.get_secret_value(),
        database=settings.postgres_db,
    )


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


async def _existing_tables(conn: asyncpg.Connection) -> dict[str, frozenset[str]]:
    rows = await conn.fetch(
        """
        SELECT n.nspname AS schema, c.relname AS tbl
          FROM pg_class c
          JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE c.relkind IN ('r', 'p', 'v', 'm')
        """
    )
    grouped: dict[str, set[str]] = {}
    for row in rows:
        grouped.setdefault(str(row["schema"]), set()).add(str(row["tbl"]))
    return {k: frozenset(v) for k, v in grouped.items()}


async def test_roles_phase_is_idempotent_and_converges() -> None:
    phase = roles_phase(get_settings())
    conn = await _conn()
    try:
        for round_index in (1, 2):
            for statement in phase.statements:
                await conn.execute(statement)
            problems = diff_role_attributes(role_specs(), await _live_roles(conn))
            assert problems == [], f"第 {round_index} 轮后仍漂移：{problems}"
    finally:
        await conn.close()


async def test_preflight_finds_no_missing_objects_at_head() -> None:
    conn = await _conn()
    try:
        assert missing_objects(await _existing_tables(conn)) == []
    finally:
        await conn.close()


async def test_grants_phase_is_idempotent() -> None:
    conn = await _conn()
    try:
        version = int(await conn.fetchval("SELECT current_setting('server_version_num')::int"))
        phase = grants_phase(server_version_num=version)
        for _ in (1, 2):
            for statement in phase.statements:
                await conn.execute(statement)
    finally:
        await conn.close()
