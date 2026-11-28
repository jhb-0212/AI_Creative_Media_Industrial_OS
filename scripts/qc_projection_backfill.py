"""调度投影的一次性回填/对账工具（``qc_maintenance.task_projection``）。

为什么需要独立工具而不是「让投影自己长出来」
--------------------------------------------
投影上线那一刻，库里已经有一批 QUEUED/RUNNING 任务，它们从未经过新代码，
投影是空的。若直接让 Scheduler 读投影，这批任务会「不存在」——不是报错，是
静默不被调度，而现象看起来和「队列空了」一模一样。

三条硬约束（缺一条就会把回填变成事故）：

1. **锁序与被回填的表一致**：先读源行再加投影，反向会与业务写入形成 AB-BA；
2. **版本只进不退**：``ON CONFLICT ... WHERE 现有版本 <= 新版本``，否则一次慢
   回填会把已经推进的投影退回旧状态；
3. **可重复执行**：失败重跑不得放大或改写既成事实——回填只写派生数据，
   同一快照重放结果相同（幂等）。

用法（只用于隔离/预发，正式库由授权流程单独执行）::

    python scripts/qc_projection_backfill.py --batch-size 200 --dry-run
    python scripts/qc_projection_backfill.py --batch-size 200

``--dry-run`` 只统计将要写/删的行数，不提交任何写入。
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from packages.shared.config import get_settings
from packages.shared.database import build_migration_url, create_sessionmaker

# 一批的行数上限：批太大持锁久、批太小回填慢。200 是「一个事务几百毫秒」的量级。
_DEFAULT_BATCH = 200

_UPSERT = """
INSERT INTO qc_maintenance.task_projection
    (task_id, tenant_id, project_id, task_status, next_action_at,
     lease_expires_at, lease_generation, entity_version, updated_at)
SELECT t.entity_id,
       t.tenant_id,
       t.project_id,
       t.status,
       CASE WHEN t.status = 'RUNNING' THEN t.lease_expires_at ELSE t.created_at END,
       t.lease_expires_at,
       t.lease_generation,
       t.entity_version,
       now()
  FROM qc_governance.qc_tasks t
 WHERE t.status IN ('QUEUED', 'RUNNING')
   AND t.entity_id > :cursor
 ORDER BY t.entity_id
 LIMIT :batch
ON CONFLICT (task_id) DO UPDATE
   SET tenant_id        = excluded.tenant_id,
       project_id       = excluded.project_id,
       task_status      = excluded.task_status,
       next_action_at   = excluded.next_action_at,
       lease_expires_at = excluded.lease_expires_at,
       lease_generation = excluded.lease_generation,
       entity_version   = excluded.entity_version,
       updated_at       = excluded.updated_at
 WHERE qc_maintenance.task_projection.entity_version <= excluded.entity_version
"""

# 终态/已删除任务的残留投影：投影只表达活动任务，残留行会让调度看到永不运行的候选。
_PRUNE = """
DELETE FROM qc_maintenance.task_projection p
 WHERE NOT EXISTS (
       SELECT 1 FROM qc_governance.qc_tasks t
        WHERE t.entity_id = p.task_id
          AND t.status IN ('QUEUED', 'RUNNING')
 )
"""

_MISMATCH = """
SELECT count(*)
  FROM qc_maintenance.task_projection p
  JOIN qc_governance.qc_tasks t ON t.entity_id = p.task_id
 WHERE p.entity_version <> t.entity_version
    OR p.lease_generation <> t.lease_generation
    OR p.task_status <> t.status
"""


async def _backfill(batch: int, dry_run: bool) -> int:
    engine = create_async_engine(build_migration_url(), pool_pre_ping=True)
    sm = create_sessionmaker(engine)
    written = 0
    cursor = ""
    try:
        while True:
            async with sm() as session:
                result = await session.execute(text(_UPSERT), {"cursor": cursor, "batch": batch})
                rows = int(result.rowcount or 0)
                last = await session.execute(
                    text(
                        "SELECT max(entity_id) FROM ("
                        "  SELECT entity_id FROM qc_governance.qc_tasks"
                        "   WHERE status IN ('QUEUED','RUNNING')"
                        "     AND entity_id > :cursor"
                        "   ORDER BY entity_id LIMIT :batch"
                        ") s"
                    ),
                    {"cursor": cursor, "batch": batch},
                )
                next_cursor = last.scalar()
                if dry_run:
                    await session.rollback()
                else:
                    await session.commit()
            written += rows
            if not next_cursor or next_cursor == cursor:
                break
            cursor = str(next_cursor)
        async with sm() as session:
            pruned = int((await session.execute(text(_PRUNE))).rowcount or 0)
            mismatched = int((await session.execute(text(_MISMATCH))).scalar_one())
            if dry_run:
                await session.rollback()
            else:
                await session.commit()
        print(
            f"projection backfill: upsert={written} pruned={pruned} "
            f"mismatch={mismatched} dry_run={dry_run}"
        )
        # 对账残留不是「差不多就行」：有差异就说明写入路径没全覆盖，
        # 必须以非零退出让调用方看见，而不是让它作为一条日志沉底。
        return mismatched
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="QC 调度投影回填/对账")
    parser.add_argument("--batch-size", type=int, default=_DEFAULT_BATCH)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if args.batch_size < 1 or args.batch_size > 5000:
        parser.error("--batch-size 必须在 1..5000 之间（太大锁表、太小无效率）")
    get_settings()
    mismatched = asyncio.run(_backfill(args.batch_size, args.dry_run))
    return 1 if mismatched else 0


if __name__ == "__main__":
    sys.exit(main())
