"""演练脚本共用的基础设施（A2 / A4）。

两个演练（迁移回滚、备份恢复）在"建 scratch 库 → 跑 → 删库"这件事上是同一套
逻辑。复制一份到第二个文件会得到两个独立的 DSN 编码规则、两套 DROP 前踢连接
的实现——一处修了口令含 ``@`` 的 bug，另一处还错着。所以抽在这里。

**这不是通用工具库**。它只服务 ``scripts/*_drill.py``，刻意不放进 ``packages/``：
- 演练脚本需要**超级用户**连接与建库/删库权限，app 运行时绝不该有这种能力；
  放进 packages 就等于给业务代码开了一条通往 DDL 的入口。
- 演练产出的证据是发布附件，不是运行时契约的一部分，不需要冻结与 CI 覆盖。
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

import asyncpg

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.shared.config import get_settings  # noqa: E402

#: alembic 子进程超时。迁移含全表回填与分区搬迁，600s 是宽松上限。
ALEMBIC_TIMEOUT_SECONDS = 600


class DrillError(RuntimeError):
    """演练断言失败（脚本逻辑/契约问题），与环境故障区分开。"""


@dataclass
class Drill:
    """演练状态收集器：断言逐条记录，失败也保留已完成部分供排查。"""

    scratch_db: str
    assertions: list[dict[str, object]] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        self.assertions.append({"name": name, "ok": ok, "detail": detail})
        if not ok:
            raise DrillError(f"{name} —— {detail}")

    def note(self, message: str) -> None:
        self.steps.append(message)
        print(f"  - {message}")


def dsn(database: str) -> str:
    """asyncpg 直连 DSN（超级用户）。

    口令与用户名按 RFC 3986 编码：直接拼接会在口令含 ``@ : /`` 时把主机解析错，
    表现为"连上了但连错库"——排查成本很高（见
    ``docs/runbooks/runbook-db-migration.md`` §1.3）。
    """
    settings = get_settings()
    user = quote(settings.postgres_user, safe="")
    password = quote(settings.postgres_password.get_secret_value(), safe="")
    return (
        f"postgresql://{user}:{password}"
        f"@{settings.postgres_host}:{settings.postgres_port}/{database}"
    )


def connection_args() -> list[str]:
    """``psql`` / ``pg_dump`` 类工具的参数（口令走 ``PGPASSWORD`` 环境变量）。

    命令行上传口令会进进程列表，同机其它用户 ``ps`` 就能看到；走环境变量是
    这些工具的标准做法。
    """
    settings = get_settings()
    return [
        "-h",
        settings.postgres_host,
        "-p",
        str(settings.postgres_port),
        "-U",
        settings.postgres_user,
    ]


def tool_env() -> dict[str, str]:
    """调用外部 CLI 时的环境（口令不落命令行，见 ``connection_args``）。"""
    return {
        **os.environ,
        "PGPASSWORD": get_settings().postgres_password.get_secret_value(),
    }


async def _terminate_connections(admin: asyncpg.Connection, database: str) -> None:
    """踢掉目标库的残留连接，否则 ``DROP DATABASE`` 会一直等。"""
    await admin.execute(
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
        "WHERE datname = $1 AND pid <> pg_backend_pid()",
        database,
    )


async def recreate_database(database: str) -> None:
    """重建空库（幂等）。"""
    admin = await asyncpg.connect(dsn("postgres"))
    try:
        await _terminate_connections(admin, database)
        await admin.execute(f'DROP DATABASE IF EXISTS "{database}"')
        await admin.execute(f'CREATE DATABASE "{database}"')
    finally:
        await admin.close()


async def drop_database(database: str) -> None:
    admin = await asyncpg.connect(dsn("postgres"))
    try:
        await _terminate_connections(admin, database)
        await admin.execute(f'DROP DATABASE IF EXISTS "{database}"')
    finally:
        await admin.close()


async def scalar(database: str, sql: str, *args: object) -> object:
    """在目标库执行单值查询后立即断开（每次短连接，避免长事务干扰 DDL）。"""
    conn = await asyncpg.connect(dsn(database))
    try:
        return await conn.fetchval(sql, *args)
    finally:
        await conn.close()


async def execute(database: str, sql: str, *args: object) -> str:
    conn = await asyncpg.connect(dsn(database))
    try:
        return await conn.execute(sql, *args)
    finally:
        await conn.close()


async def fetch(database: str, sql: str, *args: object) -> list[asyncpg.Record]:
    conn = await asyncpg.connect(dsn(database))
    try:
        return list(await conn.fetch(sql, *args))
    finally:
        await conn.close()


async def table_exists(database: str, qualified: str) -> bool:
    schema, _, table = qualified.partition(".")
    return bool(
        await scalar(
            database,
            "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = $1 AND table_name = $2)",
            schema,
            table,
        )
    )


async def count_user_tables(database: str) -> int:
    """用户表数量（排除系统 schema；含 alembic_version，它也是真实结构状态）。"""
    return int(
        await scalar(
            database,
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_type = 'BASE TABLE' "
            "AND table_schema NOT IN ('pg_catalog', 'information_schema')",
        )
    )


async def server_version(database: str) -> str:
    return str(await scalar(database, "SHOW server_version"))


def git_head() -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.stdout.strip() or "unknown"


def run_alembic(database: str, *args: str) -> str:
    """在指定库上跑 alembic（子进程 + 环境变量覆盖 ``POSTGRES_DB``）。

    用子进程而不是 ``alembic.config.main``：``env.py`` 读环境变量，子进程能保证
    每次都是干净的配置状态；同进程反复调用要处理 ``Settings`` 缓存与已建引擎，
    反而更容易测出"不是生产那条路径"。
    """
    completed = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=str(ROOT),
        env={**os.environ, "POSTGRES_DB": database},
        capture_output=True,
        text=True,
        timeout=ALEMBIC_TIMEOUT_SECONDS,
        check=False,
    )
    if completed.returncode != 0:
        raise DrillError(
            f"alembic {' '.join(args)} 失败（exit={completed.returncode}）\n"
            f"--- stdout ---\n{completed.stdout[-4000:]}\n"
            f"--- stderr ---\n{completed.stderr[-4000:]}"
        )
    return completed.stdout


def current_revision(database: str) -> str:
    """解析 ``alembic current`` 的 revision（输出里首个 12 位十六进制串）。"""
    output = run_alembic(database, "current")
    for line in output.splitlines():
        token = line.strip().split(" ")[0]
        if len(token) >= 12 and all(c in "0123456789abcdef" for c in token[:12]):
            return token[:12]
    raise DrillError(f"无法从 alembic current 输出解析 revision：\n{output}")
