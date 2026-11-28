"""由**实测结果**生成双向快照（替代按日志正则抽 verdict 的旧生成器）。

为什么要换
----------
旧 ``%TEMP%\\wp04_build_snapshot.py`` 有两个结构性缺陷：

1. 只用正则从日志抽 ``revision/parent/verdict``，日志里的 ``diff_groups`` 明细
   被丢弃，随后**无条件**写 ``"diff": {}``——17 条来自该日志的条目，``diff``
   字段按构造必然为空，不是测量结果；
2. 末尾**无条件硬覆盖** ``f8c9d0e1a2b3`` 为 ``no_restore_proven``（``diff:{}``），
   证据串指向一个全包内不存在的日志。

本生成器只做一件事：把采集脚本的**结构化结果**（含 ``diff`` 与 P→U 中间态）
原样转成快照，不做任何按 revision 的分支、不写死任何结论。缺字段即失败。

输入契约（``wp04_triage_nm_u.py`` 的结果 JSON）::

    [{"revision","parent","status","verdict","diff","diff_p_to_u","diff_u_to_d_group_count"}]

``status`` 不是 ``ok`` 的条目（父态升级失败 / 降级失败）一律记
``unproven_blocking`` 并把失败尾部写进 ``note``——失败不能记成"已证明"。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

VERDICTS = ("no_restore_proven", "restore_required", "intentional_one_way", "unproven_blocking")


def build(results: list[dict], *, run_env: dict, run_at: str, source: str, scope: str) -> dict:
    entries: list[dict] = []
    for item in results:
        revision = str(item.get("revision") or "").strip()
        parent = str(item.get("parent") or "").strip()
        if not revision or not parent:
            raise SystemExit(f"结果条目缺少 revision/parent: {item}")

        status = item.get("status")
        if status != "ok":
            entries.append(
                {
                    "revision": revision,
                    "parent": parent,
                    "verdict": "unproven_blocking",
                    "diff": {},
                    "note": f"采集未完成（status={status}）：{str(item.get('log') or '')[-600:]}",
                }
            )
            continue

        verdict = item.get("verdict")
        if verdict not in VERDICTS:
            raise SystemExit(f"{revision}: verdict 非法 {verdict!r}")
        diff = item.get("diff") or {}
        if verdict == "no_restore_proven" and diff:
            raise SystemExit(f"{revision}: 标 no_restore_proven 但 diff 非空，拒绝生成")
        entries.append(
            {
                "revision": revision,
                "parent": parent,
                "verdict": verdict,
                # 原样保留实测差异，空差异也来自实测（diff_groups=[]）
                "diff": diff,
                # 中间态证据：证明升级真的改变了权限面，而不是"什么都没干"
                "diff_p_to_u": item.get("diff_p_to_u") or {},
                "diff_u_to_d_group_count": item.get("diff_u_to_d_group_count"),
            }
        )

    entries.sort(key=lambda e: e["revision"])
    return {
        "method": (
            "P(parent) -> alembic upgrade(target) -> U 快照 -> alembic downgrade(parent) -> D 快照；"
            "规范化 ACL（aclexplode+acldefault）、pg_toast OID 抖动剔除；"
            "diff 字段来自实测结果文件，未做任何人工改写"
        ),
        "run_at": run_at,
        "run_env": run_env,
        "scope": scope,
        "source_results": source,
        "generator": "docs/audits/2026-11-28-wp04_build_snapshot_v2.py（结构化实测结果直转）",
        "results": entries,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="由实测结果生成双向快照")
    p.add_argument("--results", required=True, type=Path)
    p.add_argument("--out", required=True, type=Path)
    p.add_argument("--run-at", default="2026-11-28")
    p.add_argument("--scope", default="矩阵中全部 asymmetric revision")
    args = p.parse_args(argv)

    results = json.loads(args.results.read_text(encoding="utf-8"))
    run_env = {
        "runtime": "Docker",
        "container": os.environ.get("WP04_RUN_CONTAINER", "wp04-verify-15444"),
        "image": os.environ.get("WP04_RUN_IMAGE", "postgres:16"),
        "server_version": os.environ.get("WP04_RUN_PG_VERSION", "16.16-1.pgdg13+1"),
        "host_port": os.environ.get("WP04_RUN_PORT", "15444"),
        "code_tree": os.environ.get("WP04_RUN_TREE", "verify-311530c"),
        "evidence_grade": "NEW-MACHINE / 2026-11-28",
    }
    payload = build(
        results,
        run_env=run_env,
        run_at=args.run_at,
        source=str(args.results).replace("\\", "/"),
        scope=args.scope,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    dumped = [e["revision"] for e in payload["results"] if e["verdict"] == "unproven_blocking"]
    print(
        json.dumps(
            {
                "entries": len(payload["results"]),
                "with_real_diff": sum(1 for e in payload["results"] if e["diff"]),
                "unproven_blocking": dumped,
                "out": str(args.out),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
