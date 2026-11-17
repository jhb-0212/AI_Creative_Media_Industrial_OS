"""A4 演练：备份与恢复（Phase 1 验收 A4）。

**A4 的通过标准**

> 恢复到指定时间点后，DeliveryManifest 可重建且哈希一致。

这句话有两个可证伪的分句，脚本分别对应：

1. **"恢复后"** —— 真的做一次 `pg_dump` → `pg_restore`，然后逐表比对内容哈希。
   只写"备份策略已制定"不算证据：备份能导出但恢复后少一行 JSONB 字段、
   或序列值没回来，只有比对才发现。
2. **"可重建且哈希一致"** —— 从**恢复出来的行**重算 `compute_manifest_checksum`
   （领域层的纯函数，与创建时同一实现），与库里存的 `checksum` 比较。
   若恢复把 `rebuild_recipe` 或 `asset_refs` 的某个字符改坏，这一步立刻红。

**为什么不测 PITR**

"A4 的指定时间点"当前实现为 **pg_dump 快照点**（`pg_dump` 在单个可重复读快照上
导出，天然是一致的时点）。真正的时间点恢复（PITR）需要 WAL 归档与
`recovery_target_time`，属运维基础设施，仓库里没有；证据文件与 ADR 里都写明
这一边界，不假装覆盖。

**为什么内容哈希按 `t::text` 排序聚合**

行级比较要一个确定的序。用主键排序会在多表拼接时逐表写一套 key；用整行文本
排序与聚合是一套通用规则，且对"字段顺序变了"敏感——那正是我们想发现的破坏。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from drill_support import (
    ROOT,
    Drill,
    DrillError,
    connection_args,
    count_user_tables,
    current_revision,
    drop_database,
    execute,
    fetch,
    git_head,
    recreate_database,
    run_alembic,
    scalar,
    server_version,
    tool_env,
)

#: 本演练依赖的 head（与 A2 同源）。head 前进后需复核种子数据的列是否仍匹配。
_EXPECTED_HEAD = "f1a2b3c4d5e6"

_SOURCE_PREFIX = "ai_film_drill_a4src_"
_TARGET_PREFIX = "ai_film_drill_a4dst_"

_TENANT_ID = "22222222-2222-2222-2222-222222222222"
_MANIFEST_ID = "manifest_drill_001"

#: 清单的"可重建内容"。刻意包含**乱序**的引用列表与含中文、换行的 recipe：
#: 排序规范化与 UTF-8 编码是最容易被备份/恢复破坏的两处，正常顺序的英文短串
#: 测不出来。
_ASSET_REFS = ["asset_v2", "asset_v1"]
_RIGHTS_REFS = ["rights_b", "rights_a"]
_REBUILD_RECIPE = "steps:\n  1. 拼接 shot_001..shot_012\n  2. 混音 master_v3\n  3. 烧字幕 zh-CN"


def _require_tools() -> None:
    missing = [name for name in ("pg_dump", "pg_restore") if not shutil.which(name)]
    if missing:
        raise DrillError(
            f"缺少 {missing}；A4 演练必须用真实 pg_dump/pg_restore，不能用"
            "「导出 SELECT 结果」替代（那测不到 schema、约束、序列与 JSONB 编码）"
        )


async def _content_hashes(database: str) -> dict[str, str]:
    """逐表内容哈希：对每张用户表算 ``md5`` 排序聚合后的整表文本。"""
    tables = await fetch(
        database,
        "SELECT table_schema, table_name FROM information_schema.tables "
        "WHERE table_type = 'BASE TABLE' "
        "AND table_schema NOT IN ('pg_catalog', 'information_schema') "
        "ORDER BY table_schema, table_name",
    )
    hashes: dict[str, str] = {}
    for row in tables:
        schema = str(row["table_schema"])
        table = str(row["table_name"])
        digest = await scalar(
            database,
            f'SELECT md5(coalesce(string_agg(t::text, E\'\\n\' ORDER BY t::text), \'\')) '
            f'FROM "{schema}"."{table}" t',
        )
        hashes[f"{schema}.{table}"] = str(digest)
    return hashes


async def _seed_manifest(database: str) -> str:
    """写入一条校验和自洽的交付清单，返回其 checksum。

    校验和用**领域层的同一个纯函数**算，不是随便填一个哈希——否则恢复后
    重算必然不等，"哈希一致"这条断言就变成了对拍两个我随手写的值。
    """
    sys.path.insert(0, str(ROOT))
    from services.asset_service.domain.delivery_manifest import (
        compute_manifest_checksum,
    )

    checksum = compute_manifest_checksum(
        asset_refs=_ASSET_REFS,
        spec_ref="spec_v1",
        qc_final_ref="gate_decision_003",
        picture_lock_ref="gate_decision_002",
        rights_evidence_refs=_RIGHTS_REFS,
        rebuild_recipe=_REBUILD_RECIPE,
    )
    await execute(
        database,
        "INSERT INTO asset_service.delivery_manifests ("
        "entity_id, entity_type, schema_version, entity_version, status,"
        " owner_module, tenant_id, organization_id, created_at, created_by,"
        " updated_at, audit_ref, project_id, asset_refs, checksum,"
        " picture_lock_ref, qc_final_ref, rebuild_recipe, rights_evidence_refs,"
        " spec_ref"
        ") VALUES ($1, 'DeliveryManifest', 205, 1, 'DRAFT', 'asset_service',"
        " $2, $2, now(), 'drill', now(), NULL, 'proj_drill', $3::jsonb, $4,"
        " 'gate_decision_002', 'gate_decision_003', $5, $6::jsonb, 'spec_v1')",
        _MANIFEST_ID,
        _TENANT_ID,
        json.dumps(_ASSET_REFS),
        checksum,
        _REBUILD_RECIPE,
        json.dumps(_RIGHTS_REFS),
    )
    return checksum


async def _recompute_from_restored(database: str) -> tuple[str, str]:
    """从恢复出来的行重算 checksum，返回 (存的, 重算的)。

    刻意直接从 SQL 读列再用领域函数重算，而不是 ``SELECT checksum`` 就当通过：
    "可重建"的语义是**内容足以重算出同一个哈希**，不是"那一列还在"。
    """
    row = await fetch(
        database,
        "SELECT asset_refs, checksum, picture_lock_ref, qc_final_ref,"
        " rebuild_recipe, rights_evidence_refs, spec_ref"
        " FROM asset_service.delivery_manifests WHERE entity_id = $1",
        _MANIFEST_ID,
    )
    if not row:
        raise DrillError("恢复后的库里找不到哨兵清单（数据丢失）")
    record = row[0]

    sys.path.insert(0, str(ROOT))
    from services.asset_service.domain.delivery_manifest import (
        compute_manifest_checksum,
    )

    recomputed = compute_manifest_checksum(
        asset_refs=json.loads(record["asset_refs"]),
        spec_ref=str(record["spec_ref"]),
        qc_final_ref=str(record["qc_final_ref"]),
        picture_lock_ref=str(record["picture_lock_ref"]),
        rights_evidence_refs=json.loads(record["rights_evidence_refs"]),
        rebuild_recipe=str(record["rebuild_recipe"]),
    )
    return str(record["checksum"]), recomputed


def _run_tool(args: list[str]) -> None:
    completed = subprocess.run(
        args, capture_output=True, text=True, env=tool_env(), check=False
    )
    if completed.returncode != 0:
        raise DrillError(
            f"{Path(args[0]).name} 失败（exit={completed.returncode}）\n"
            f"--- stdout ---\n{completed.stdout[-4000:]}\n"
            f"--- stderr ---\n{completed.stderr[-4000:]}"
        )


async def run_drill(*, keep: bool, keep_dump: bool) -> dict[str, object]:
    _require_tools()
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    source = f"{_SOURCE_PREFIX}{stamp}"
    target = f"{_TARGET_PREFIX}{stamp}"
    drill = Drill(scratch_db=source)

    dump_path = Path(tempfile.gettempdir()) / f"{source}.dump"
    print(f"[A4] 源库：{source}　恢复目标：{target}")
    print(f"[A4] 备份文件：{dump_path}")

    await recreate_database(source)
    await recreate_database(target)
    try:
        # --- 1. 造数据 ----------------------------------------------------
        run_alembic(source, "upgrade", "head")
        head = current_revision(source)
        drill.check(
            "head 与脚本期望一致",
            head == _EXPECTED_HEAD,
            f"实际 {head}，期望 {_EXPECTED_HEAD}",
        )
        checksum = await _seed_manifest(source)
        tables = await count_user_tables(source)
        drill.note(f"种子完成：清单 {_MANIFEST_ID} checksum={checksum[:12]}…；表 {tables} 张")

        # --- 2. 备份 ------------------------------------------------------
        _run_tool(
            [
                "pg_dump",
                *connection_args(),
                "-d",
                source,
                "-Fc",
                "-f",
                str(dump_path),
            ]
        )
        size = dump_path.stat().st_size
        drill.check(
            "备份文件已生成且非空（空文件是最容易被忽略的失败）",
            size > 0,
            f"{size} 字节",
        )
        drill.note(f"pg_dump 完成：{size} 字节")

        # --- 3. 恢复 ------------------------------------------------------
        _run_tool(
            [
                "pg_restore",
                *connection_args(),
                "-d",
                target,
                str(dump_path),
            ]
        )
        target_tables = await count_user_tables(target)
        drill.check(
            "恢复后表数量与源库一致",
            target_tables == tables,
            f"源 {tables} → 恢复 {target_tables}",
        )
        drill.note(f"pg_restore 完成：表 {target_tables} 张")

        # --- 4. 逐表内容比对 ----------------------------------------------
        source_hashes = await _content_hashes(source)
        target_hashes = await _content_hashes(target)
        drill.check(
            "恢复后表集合与源库一致",
            set(source_hashes) == set(target_hashes),
            f"仅源库有 {sorted(set(source_hashes) - set(target_hashes))}；"
            f"仅恢复库有 {sorted(set(target_hashes) - set(source_hashes))}",
        )
        mismatched = [
            f"{name}（源 {source_hashes[name][:12]}… / 恢复 {target_hashes[name][:12]}…）"
            for name in source_hashes
            if source_hashes[name] != target_hashes[name]
        ]
        drill.check(
            "逐表内容哈希一致（不止行数，是整行内容）",
            mismatched == [],
            f"不一致的表：{mismatched}",
        )
        drill.note(f"逐表哈希比对：{len(source_hashes)} 张表全部一致")

        # --- 5. 清单可重建且哈希一致 --------------------------------------
        stored, recomputed = await _recompute_from_restored(target)
        drill.check(
            "恢复后 DeliveryManifest 可重建且哈希一致",
            stored == recomputed,
            f"存的 {stored[:16]}… / 重算 {recomputed[:16]}…",
        )
        drill.check(
            "重算哈希等于备份前的值（跨备份/恢复未漂移）",
            recomputed == checksum,
            f"备份前 {checksum[:16]}… / 恢复后重算 {recomputed[:16]}…",
        )
        drill.note(f"清单重建校验和一致：{recomputed[:16]}…")

        return {
            "checklist_item": "A4",
            "title": "备份与恢复演练",
            "generated_at": datetime.now(UTC).isoformat(),
            "commit": git_head(),
            "server_version": await server_version(target),
            "source_database": source,
            "target_database": target,
            "dump_bytes": size,
            "table_count": tables,
            "tables_compared": len(source_hashes),
            "manifest_checksum": checksum,
            "assertions": drill.assertions,
            "steps": drill.steps,
            "limitations": [
                "只覆盖 pg_dump 快照点恢复，不覆盖 PITR（WAL 归档 + "
                "recovery_target_time）：仓库内无 WAL 归档基础设施，属运维范围。",
                "只覆盖逻辑备份（-Fc）。物理备份/基础备份 + WAL 属 Phase 4 灾备"
                "（phase_map.yaml Phase 4）范围。",
                "未覆盖恢复过程中的部分失败（pg_restore 非 0 退出且残留半套结构"
                "的处置）；runbook §2 要求「先备份再降级」，但恢复失败的分支尚无"
                "演练。",
                "数据量为种子数据量级，未覆盖生产规模下的备份耗时与恢复 RTO。",
            ],
        }
    finally:
        for database in (source, target):
            if keep:
                print(f"[A4] 保留库：{database}")
            else:
                await drop_database(database)
                print(f"[A4] 已删除库：{database}")
        if keep_dump:
            print(f"[A4] 保留备份文件：{dump_path}")
        elif dump_path.exists():
            dump_path.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description="A4 备份与恢复演练")
    parser.add_argument("--keep", action="store_true", help="保留演练库（排查用）")
    parser.add_argument("--keep-dump", action="store_true", help="保留备份文件")
    parser.add_argument("--json", type=Path, default=None, help="证据 JSON 输出路径")
    args = parser.parse_args()

    try:
        report = asyncio.run(run_drill(keep=args.keep, keep_dump=args.keep_dump))
    except DrillError as exc:
        print(f"\n[FAIL] {exc}")
        return 1
    except Exception as exc:
        print(f"\n[ERROR] 演练未能执行：{type(exc).__name__}: {exc}")
        return 2

    print("\n[PASS] A4 演练通过：")
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
