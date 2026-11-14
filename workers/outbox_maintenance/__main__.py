"""Outbox 分区创建与保留期维护 CLI（R06）。

决策：docs/adr/ADR-R06-2-outbox-retention-partitioning.md
运维手册：docs/runbooks/runbook-outbox-retention.md

为什么是一个独立 CLI、而不是 API/relay 的一部分：
- 它需要 DDL 权限（CREATE TABLE / DETACH / DROP）。应用路径（app_role）只有
  父表的 SELECT/INSERT/UPDATE，把 DDL 权限给应用角色才是真正的权限倒退。
- 保留期操作是**运维决策**，不该由业务进程按负载自动触发：一次误删不可恢复。

安全默认值：
- 不带 --apply 只打印计划（dry-run），不做任何变更；
- 摘除分区前逐个检查「是否还有未投递事件」，有则拒绝并返回非 0；
- 摘除前可先归档（--archive-dir）：导出 CSV.gz + sha256 + manifest，导出失败即中止；
- 摘除后默认只改名保留（outbox_events_archived_yYYYYMM），--drop-detached 才删除。

自动化（避免「机制建了但没人跑」）：
- `--every-hours N` 进入常驻模式，每 N 小时执行一轮（默认 24）；
- 每轮结果写入 `outbox.maintenance_runs`（含 dry-run 与失败），运维侧只查这张表
  就能判断「上次成功是什么时候」，不依赖任何人记得。

典型用法：
    python -m workers.outbox_maintenance                       # 只看计划
    python -m workers.outbox_maintenance --apply               # 建分区 + 摘除
    python -m workers.outbox_maintenance --apply --archive-dir /backup/outbox
    python -m workers.outbox_maintenance --apply --every-hours 24   # 常驻
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncTransaction, create_async_engine

from packages.shared.config import get_settings
from packages.shared.database import build_migration_url
from packages.shared.logging import get_logger, setup_logging
from workers.outbox_maintenance.archive import export_partition

logger = get_logger(__name__)

SCHEMA = "outbox"
PARENT = "outbox_events"
DEFAULT_PARTITION = "outbox_events_default"
RUNS_TABLE = "maintenance_runs"
_PARTITION_RE = re.compile(r"outbox_events_y(\d{4})(\d{2})")
_ARCHIVED_PREFIX = "outbox_events_archived_y"

_LIST_PARTITIONS = text(
    """
    SELECT c.relname
    FROM pg_inherits i
    JOIN pg_class c ON c.oid = i.inhrelid
    JOIN pg_class p ON p.oid = i.inhparent
    JOIN pg_namespace n ON n.oid = p.relnamespace
    WHERE n.nspname = :schema AND p.relname = :parent
    ORDER BY c.relname
    """
)


def month_start(value: datetime) -> datetime:
    """归一到一个月的起点（UTC，含时区）。"""
    return datetime(value.year, value.month, 1, tzinfo=UTC)


def add_months(value: datetime, months: int) -> datetime:
    month_index = value.year * 12 + (value.month - 1) + months
    year, month = divmod(month_index, 12)
    return datetime(year, month + 1, 1, tzinfo=UTC)


def partition_name(month: datetime) -> str:
    return f"{PARENT}_y{month.strftime('%Y%m')}"


def parse_partition_month(name: str) -> datetime | None:
    """从分区名解析出所属月份；非月份分区（如 default）返回 None。"""
    match = _PARTITION_RE.fullmatch(name)
    if match is None:
        return None
    year, month = int(match.group(1)), int(match.group(2))
    return datetime(year, month, 1, tzinfo=UTC)


@dataclass(frozen=True)
class Partition:
    """一个月份分区及其时间边界（右开区间）。"""

    name: str
    lower: datetime
    upper: datetime


@dataclass
class RunResult:
    """一轮维护的结果（用于写 maintenance_runs 与退出码）。"""

    created: int = 0
    detached: int = 0
    refused: int = 0
    status: str = "ok"
    error: str | None = None


def plan_create(existing: set[str], today: datetime, ahead_months: int) -> list[Partition]:
    """需要补建的月份分区：本月 → 本月 + ahead（缺失才建）。"""
    current = month_start(today)
    planned: list[Partition] = []
    for offset in range(ahead_months + 1):
        month = add_months(current, offset)
        name = partition_name(month)
        if name in existing:
            continue
        planned.append(Partition(name=name, lower=month, upper=add_months(month, 1)))
    return planned


def plan_retention(
    existing: set[str], today: datetime, retain_months: int
) -> list[Partition]:
    """整月早于保留窗口的分区（右开区间上界 <= 保留下界才可摘除）。"""
    cutoff = add_months(month_start(today), -retain_months)
    candidates: list[Partition] = []
    for name in sorted(existing):
        month = parse_partition_month(name)
        if month is None:
            continue
        upper = add_months(month, 1)
        if upper <= cutoff:
            candidates.append(Partition(name=name, lower=month, upper=upper))
    return candidates


async def list_partitions(conn: AsyncConnection) -> set[str]:
    rows = (
        await conn.execute(_LIST_PARTITIONS, {"schema": SCHEMA, "parent": PARENT})
    ).scalars()
    return set(rows)


async def _begin(conn: AsyncConnection) -> AsyncTransaction:
    """开启一个干净事务。

    SQLAlchemy 2 的连接在一条 SELECT 后会隐式开启事务；直接 ``conn.begin()``
    会报「已初始化 Transaction」。这里先结束隐式事务（只读语句，commit 无副作用），
    保证「检查 → DDL → 回灌」始终在同一个显式事务里。
    """
    if conn.in_transaction():
        await conn.commit()
    return conn.begin()


async def ensure_partition(conn: AsyncConnection, part: Partition) -> int:
    """在事务内创建月份分区，并先搬走 DEFAULT 分区里落在该区间的行。

    顺序不能反：DEFAULT 分区已有区间内数据时，直接 CREATE PARTITION 会被
    PostgreSQL 拒绝（默认分区约束冲突）。先把行暂存到 TEMP 表 → 删 DEFAULT 行
    → 建分区 → 回灌，全程一个事务，失败即整体回滚。
    """
    lower, upper = part.lower.isoformat(), part.upper.isoformat()
    moved = 0
    async with await _begin(conn):
        await conn.execute(
            text(
                f"CREATE TEMP TABLE _outbox_moved ON COMMIT DROP AS "
                f"SELECT * FROM {SCHEMA}.{DEFAULT_PARTITION} "
                f"WHERE occurred_at >= '{lower}' AND occurred_at < '{upper}'"
            )
        )
        moved = int(
            (
                await conn.execute(text("SELECT count(*) FROM _outbox_moved"))
            ).scalar_one()
        )
        if moved:
            logger.warning(
                "outbox_default_partition_rows_moved",
                partition=part.name,
                rows=moved,
            )
            await conn.execute(
                text(
                    f"DELETE FROM {SCHEMA}.{DEFAULT_PARTITION} "
                    f"WHERE occurred_at >= '{lower}' AND occurred_at < '{upper}'"
                )
            )
        await conn.execute(
            text(
                f"CREATE TABLE {SCHEMA}.{part.name} "
                f"PARTITION OF {SCHEMA}.{PARENT} "
                f"FOR VALUES FROM ('{lower}') TO ('{upper}')"
            )
        )
        if moved:
            await conn.execute(
                text(f"INSERT INTO {SCHEMA}.{PARENT} SELECT * FROM _outbox_moved")
            )
    return moved


async def count_unpublished(conn: AsyncConnection, name: str) -> int:
    return int(
        (
            await conn.execute(
                text(f"SELECT count(*) FROM {SCHEMA}.{name} WHERE published_at IS NULL")
            )
        ).scalar_one()
    )


async def detach_partition(
    conn: AsyncConnection,
    part: Partition,
    *,
    drop: bool,
    archive_target: str | None = None,
) -> None:
    """摘除分区：先（可选）归档，再摘除；默认改名保留，drop=True 才删除。"""
    unpublished = await count_unpublished(conn, part.name)
    if unpublished:
        raise RuntimeError(
            f"拒绝摘除 {part.name}：仍有 {unpublished} 条未投递事件。"
            " 请先让 relay 排空（或调大保留期），不要用删除换空间。"
        )
    if drop and archive_target is None:
        raise RuntimeError(
            f"拒绝删除 {part.name}：指定 --drop-detached 时必须同时给 --archive-dir，"
            "先归档再释放空间（没有可校验副本的删除不可恢复）。"
        )
    if archive_target is not None:
        await export_partition(conn, part.name, archive_target)

    archived = f"{_ARCHIVED_PREFIX}{part.lower.strftime('%Y%m')}"
    async with await _begin(conn):
        await conn.execute(
            text(f"ALTER TABLE {SCHEMA}.{PARENT} DETACH PARTITION {SCHEMA}.{part.name}")
        )
        await conn.execute(
            text(f"ALTER TABLE {SCHEMA}.{part.name} RENAME TO {archived}")
        )
    if drop:
        async with await _begin(conn):
            await conn.execute(text(f"DROP TABLE {SCHEMA}.{archived}"))
            logger.info("outbox_partition_dropped", partition=archived)


async def record_run(
    conn: AsyncConnection, result: RunResult, *, applied: bool, started_at: datetime
) -> None:
    """把一轮结果写入 outbox.maintenance_runs（编排/告警据此判断新鲜度）。"""
    async with await _begin(conn):
        await conn.execute(
            text(
                f"INSERT INTO {SCHEMA}.{RUNS_TABLE} "
                "(started_at, finished_at, applied, created, detached, refused,"
                " status, error) "
                "VALUES (:started, now(), :applied, :created, :detached, :refused,"
                " :status, :error)"
            ),
            {
                "started": started_at,
                "applied": applied,
                "created": result.created,
                "detached": result.detached,
                "refused": result.refused,
                "status": result.status,
                "error": result.error,
            },
        )


async def run_once(args: argparse.Namespace) -> RunResult:
    """执行一轮：列分区 → 建缺失 → 摘除过期（拒绝的单独计数）。"""
    result = RunResult()
    today = datetime.now(UTC)
    engine = create_async_engine(build_migration_url())
    try:
        async with engine.connect() as conn:
            existing = await list_partitions(conn)
            to_create = plan_create(existing, today, args.ahead)
            to_detach = plan_retention(existing, today, args.retain_months)

            logger.info(
                "outbox_maintenance_plan",
                existing=len(existing),
                create=[p.name for p in to_create],
                detach=[p.name for p in to_detach],
                apply=args.apply,
                drop_detached=args.drop_detached,
            )

            if not args.apply:
                print("dry-run：以下操作未执行（加 --apply 才执行）")
                for part in to_create:
                    print(
                        f"  + {part.name} [{part.lower:%Y-%m-%d}, {part.upper:%Y-%m-%d})"
                    )
                for part in to_detach:
                    print(f"  - {part.name}（摘除，--drop-detached 才删除）")
                result.status = "dry_run"
                return result

            for part in to_create:
                moved = await ensure_partition(conn, part)
                result.created += 1
                print(f"created {part.name} (moved_from_default={moved})")

            archive_target = args.archive_dir or None
            for part in to_detach:
                try:
                    await detach_partition(
                        conn, part, drop=args.drop_detached, archive_target=archive_target
                    )
                    result.detached += 1
                    print(f"detached {part.name} (drop={args.drop_detached})")
                except RuntimeError as exc:
                    logger.error("outbox_retention_refused", detail=str(exc))
                    print(f"REFUSED {part.name}: {exc}")
                    result.refused += 1
            if result.refused:
                result.status = "refused"
            return result
    finally:
        await engine.dispose()


async def _record(args: argparse.Namespace, result: RunResult, started_at: datetime) -> None:
    """独立连接写审计行：即使本轮中途报错，也不该因为同一连接状态而丢记录。"""
    applied = bool(args.apply)

    async def _write() -> None:
        engine = create_async_engine(build_migration_url())
        try:
            async with engine.connect() as conn:
                await record_run(
                    conn, result, applied=applied, started_at=started_at
                )
        finally:
            await engine.dispose()

    try:
        await _write()
    except Exception:
        logger.error("outbox_maintenance_record_failed", exc_info=True)


async def _run(args: argparse.Namespace) -> int:
    settings = get_settings()
    setup_logging(settings.log_level)
    exit_code = 0
    while True:
        started_at = datetime.now(UTC)
        try:
            result = await run_once(args)
        except Exception as exc:  # 常驻模式下不能因为一轮失败就退出
            logger.error("outbox_maintenance_failed", exc_info=True)
            result = RunResult(status="error", error=str(exc)[:2000])
        await _record(args, result, started_at)
        exit_code = 1 if result.status in {"refused", "error"} else 0
        if args.every_hours <= 0:
            return exit_code
        logger.info(
            "outbox_maintenance_sleep", hours=args.every_hours, next_in_seconds=int(
                args.every_hours * 3600
            )
        )
        await asyncio.sleep(args.every_hours * 3600)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="workers.outbox_maintenance",
        description="Outbox 分区创建与保留期维护（默认 dry-run）",
    )
    parser.add_argument("--ahead", type=int, default=3, help="预建未来月份数（默认 3）")
    parser.add_argument(
        "--retain-months", type=int, default=6, help="保留月数，超过则摘除（默认 6）"
    )
    parser.add_argument("--apply", action="store_true", help="真正执行（默认只打印计划）")
    parser.add_argument(
        "--archive-dir",
        default="",
        help="摘除前导出 CSV.gz + sha256 + manifest 的目录（强烈建议给）",
    )
    parser.add_argument(
        "--drop-detached",
        action="store_true",
        help="摘除后删除分区表（必须同时给 --archive-dir）",
    )
    parser.add_argument(
        "--every-hours",
        type=float,
        default=0,
        help="常驻模式：每 N 小时执行一轮；0 表示只跑一次（默认 0）",
    )
    args = parser.parse_args(argv)
    if args.ahead < 0 or args.retain_months < 1:
        parser.error("--ahead 必须 >= 0，--retain-months 必须 >= 1")
    if args.every_hours < 0:
        parser.error("--every-hours 不能为负")
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
