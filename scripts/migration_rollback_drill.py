"""A2 演练：Schema 迁移与回滚（Phase 1 验收 A2）。

**为什么需要脚本而不是一段说明**

A2 的通过标准是"迁移后对象的 `schema_version` 与 `entity_version` 语义不变；
回滚不丢已提交的版本记录"。这句话里有两个词只能靠**真实数据库**证明：
"迁移后"和"回滚"。读一遍 `downgrade()` 源码不算证据——降级顺序写错、约束依赖
漏删、`alembic check` 有漂移，都只有跑一遍才看得见。

因此本脚本在一个**一次性 scratch 库**里做四件事：

1. `alembic upgrade head` —— 从空库建到最新；
2. 写入一条哨兵记录（真实实体行，带 `schema_version` / `entity_version`）；
3. `alembic downgrade -1` —— 只降**一步**，然后断言哨兵记录仍在、版本号未变、
   被降级迁移建的表确实消失（结构真的回退了，不是空操作）；
4. `alembic upgrade head` 再升回来，断言哨兵仍在、`alembic check` 无漂移。

**为什么不演到 base**（诚实登记的边界）

数据库角色（`svc_project` / `svc_asset` …）是**跨库对象**，它们的 `DROP ROLE`
写在 `c2d3e4f5a6b7` 的 `downgrade` 里。若在共享集群上降到 base，会把同集群里
其它库（开发库、测试库）正在使用的角色删掉。降 base 演练需要独立集群，当前
环境没有；因此本演练只覆盖"降一步"，并在证据文件里把这个边界写清楚——而不是
假装覆盖了全部。

**为什么期望 head 写死在脚本里**

演练必须能失败。若只断言"降级后再升级能成功"，那么在 head 前进到某个空操作
迁移（例如只改注释）时，演练会照常通过却什么都没验证——典型的空跑绿。
所以脚本钉住 `_EXPECTED_HEAD` 与被降级迁移建的表名：head 一变就**报错退出**，
强迫维护者同步更新断言目标。

用法：

    python scripts/migration_rollback_drill.py
    python scripts/migration_rollback_drill.py --keep          # 保留 scratch 库
    python scripts/migration_rollback_drill.py --json <path>   # 落盘证据
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from drill_support import (
    Drill,
    DrillError,
    count_user_tables,
    current_revision,
    drop_database,
    execute,
    git_head,
    recreate_database,
    run_alembic,
    scalar,
    server_version,
    table_exists,
)

#: 本演练针对的 head。head 前进后必须同步更新（见模块 docstring 末段）。
_EXPECTED_HEAD = "f1a2b3c4d5e6"

#: 被 head 迁移创建的表（降级后必须消失）。与 ``_EXPECTED_HEAD`` 同源。
_HEAD_CREATED_TABLE = "qc_governance.qc_review_sessions"

#: scratch 库名前缀，带时间戳避免与手工排查用的库撞名。
_SCRATCH_PREFIX = "ai_film_drill_"

#: 哨兵：一条真实的 projects 行。选 projects 是因为它由最早的迁移创建，
#: 降一步（只影响最新迁移）时必然不受影响——这正是 A2 要的"回滚不丢
#: **已提交**的版本记录"。
_SENTINEL_STATUS = "DRAFT"
_SENTINEL_SCHEMA_VERSION = 205
_SENTINEL_ENTITY_VERSION = 7
_TENANT_ID = "11111111-1111-1111-1111-111111111111"


async def _insert_sentinel(database: str, entity_id: str) -> None:
    await execute(
        database,
        "INSERT INTO project_planning.projects ("
        "entity_id, entity_type, schema_version, entity_version, status,"
        " owner_module, tenant_id, organization_id, created_at, created_by,"
        " updated_at, project_name, project_tier"
        ") VALUES ($1, 'Project', $2, $3, $4, 'project_planning',"
        " $5, $5, now(), 'drill', now(), '演练项目', 'STANDARD')",
        entity_id,
        _SENTINEL_SCHEMA_VERSION,
        _SENTINEL_ENTITY_VERSION,
        _SENTINEL_STATUS,
        _TENANT_ID,
    )


async def _read_sentinel(database: str, entity_id: str) -> tuple[int, int, str] | None:
    row = await scalar(
        database,
        "SELECT (schema_version, entity_version, status)::text "
        "FROM project_planning.projects WHERE entity_id = $1",
        entity_id,
    )
    if row is None:
        return None
    # postgres 的复合类型文本形如 "(205,7,DRAFT)"
    parts = str(row).strip("()").split(",")
    return int(parts[0]), int(parts[1]), parts[2]


async def run_drill(*, keep: bool) -> dict[str, object]:
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    scratch = f"{_SCRATCH_PREFIX}a2_{stamp}"
    drill = Drill(scratch_db=scratch)
    entity_id = f"drill_sentinel_{stamp}"

    print(f"[A2] scratch 库：{scratch}")
    await recreate_database(scratch)
    try:
        # --- 1. 升到 head -------------------------------------------------
        run_alembic(scratch, "upgrade", "head")
        head = current_revision(scratch)
        drill.note(f"upgrade head → revision={head}")
        drill.check(
            "head 与脚本期望一致（否则本演练会退化成空跑）",
            head == _EXPECTED_HEAD,
            f"实际 {head}，期望 {_EXPECTED_HEAD}；head 前进后请同步更新 "
            "_EXPECTED_HEAD 与 _HEAD_CREATED_TABLE",
        )
        drill.check(
            "被降级迁移建的表在 head 上存在",
            await table_exists(scratch, _HEAD_CREATED_TABLE),
            _HEAD_CREATED_TABLE,
        )
        tables_at_head = await count_user_tables(scratch)

        # --- 2. 写哨兵 ----------------------------------------------------
        await _insert_sentinel(scratch, entity_id)
        at_head = await _read_sentinel(scratch, entity_id)
        drill.check(
            "哨兵记录写入成功（schema_version / entity_version 符合预期）",
            at_head == (
                _SENTINEL_SCHEMA_VERSION,
                _SENTINEL_ENTITY_VERSION,
                _SENTINEL_STATUS,
            ),
            f"实际 {at_head}",
        )
        drill.note(f"哨兵写入：{at_head}；用户表 {tables_at_head} 张")

        # --- 3. 降一步 ----------------------------------------------------
        run_alembic(scratch, "downgrade", "-1")
        after_down = current_revision(scratch)
        drill.note(f"downgrade -1 → revision={after_down}")
        drill.check(
            "降级后 revision 确实回退（不是空操作）",
            after_down != head,
            f"降级前后同为 {head}",
        )
        drill.check(
            "被降级迁移建的表已消失（结构真的回退）",
            not await table_exists(scratch, _HEAD_CREATED_TABLE),
            _HEAD_CREATED_TABLE,
        )
        tables_after_down = await count_user_tables(scratch)
        drill.check(
            "降级删除了表（表数下降）",
            tables_after_down < tables_at_head,
            f"降级前 {tables_at_head} → 降级后 {tables_after_down}",
        )
        survived = await _read_sentinel(scratch, entity_id)
        drill.check(
            "回滚不丢已提交的版本记录（哨兵 schema_version / entity_version 未变）",
            survived == at_head,
            f"回滚前 {at_head} → 回滚后 {survived}",
        )

        # --- 4. 升回来 ----------------------------------------------------
        run_alembic(scratch, "upgrade", "head")
        reup = current_revision(scratch)
        drill.note(f"upgrade head（二次）→ revision={reup}")
        drill.check("二次升级回到同一 head", reup == head, f"{reup} != {head}")
        drill.check(
            "二次升级后表结构恢复",
            await table_exists(scratch, _HEAD_CREATED_TABLE),
            _HEAD_CREATED_TABLE,
        )
        tables_reup = await count_user_tables(scratch)
        drill.check(
            "二次升级后表数恢复",
            tables_reup == tables_at_head,
            f"{tables_reup} != {tables_at_head}",
        )
        drill.check(
            "二次升级后哨兵记录仍在且版本未变",
            await _read_sentinel(scratch, entity_id) == at_head,
            "哨兵丢失或版本被改写",
        )

        # --- 5. 无漂移 ----------------------------------------------------
        run_alembic(scratch, "check")
        drill.note("alembic check：无结构漂移")

        return {
            "checklist_item": "A2",
            "title": "Schema 迁移与回滚演练",
            "generated_at": datetime.now(UTC).isoformat(),
            "commit": git_head(),
            "scratch_database": scratch,
            "server_version": await server_version(scratch),
            "head_revision": head,
            "revision_after_downgrade": after_down,
            "tables_at_head": tables_at_head,
            "tables_after_downgrade": tables_after_down,
            "sentinel": {"entity_id": entity_id, "value": list(at_head)},
            "assertions": drill.assertions,
            "steps": drill.steps,
            "limitations": [
                "只演练降一步（downgrade -1）。降到 base 会执行 c2d3e4f5a6b7 的 "
                "DROP ROLE，而角色是跨库对象——在共享集群上降 base 会删掉其它库"
                "正在使用的角色。降 base 演练需要独立集群。",
                "未覆盖生产规模数据量下的迁移耗时与锁等待（属压测范围，"
                "见 phase1-resilience.md）。",
                "未覆盖「迁移中途失败（部分应用）」后的恢复；alembic 在 "
                "PostgreSQL 上是单事务，天然回滚，但本演练未构造该场景。",
            ],
        }
    finally:
        if keep:
            print(f"[A2] 保留 scratch 库：{scratch}")
        else:
            await drop_database(scratch)
            print(f"[A2] 已删除 scratch 库：{scratch}")


def main() -> int:
    parser = argparse.ArgumentParser(description="A2 迁移回滚演练")
    parser.add_argument("--keep", action="store_true", help="保留 scratch 库（排查用）")
    parser.add_argument("--json", type=Path, default=None, help="证据 JSON 输出路径")
    args = parser.parse_args()

    try:
        report = asyncio.run(run_drill(keep=args.keep))
    except DrillError as exc:
        print(f"\n[FAIL] {exc}")
        return 1
    except Exception as exc:  # 环境故障（连不上库等）与断言失败要区分开
        print(f"\n[ERROR] 演练未能执行：{type(exc).__name__}: {exc}")
        return 2

    print("\n[PASS] A2 演练通过：")
    for item in report["assertions"]:
        print(f"  ✓ {item['name']}")

    if args.json is not None:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"\n证据已写入：{args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
