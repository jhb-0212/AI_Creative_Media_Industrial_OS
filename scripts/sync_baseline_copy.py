"""基线主文档权威源单一化工具。

背景（ADR-R27 A5 residual_risk 收口）：
    基线主文档存在两份——仓库冻结副本（``docs/baseline/AI影视工业化生产平台V2.0.5.md``，
    freeze_manifest 逐字节校验的权威源）与工作台根目录的历史主文档副本
    （工作流工具使用）。两者必须**逐字节一致**，否则「符合 V2.0.5」的宣称
    就分叉成两个事实。

决策：**冻结副本是唯一权威源**。修订流程（contract_amendments 登记 → 改
冻结副本 → frozen_inventory 哈希重建 → freeze_manifest --write）只作用于
冻结副本；外部副本永远是被同步的投影，不承载任何独立修订。

用法：
    python scripts/sync_baseline_copy.py --check   # 校验两份一致（默认）
    python scripts/sync_baseline_copy.py --sync    # 以冻结副本覆写外部副本

为什么不进 CI：外部副本在仓库外（git 不可见），CI 环境没有它；这是本地
运维工具，属于「修订后人工收尾」的一部分。
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
#: 权威源（冻结副本，freeze_manifest 逐字节校验的对象）。
AUTHORITY = ROOT / "docs" / "baseline" / "AI影视工业化生产平台V2.0.5.md"
#: 投影副本（工作台根目录的历史主文档）。
PROJECTION = ROOT.parent / "AI影视工业化生产平台V2.0.5.md"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check() -> int:
    """校验投影副本与权威源逐字节一致。"""
    problems: list[str] = []
    if not AUTHORITY.exists():
        print(f"[FAIL] 权威源缺失：{AUTHORITY}")
        return 1
    if not PROJECTION.exists():
        print(f"[FAIL] 投影副本缺失：{PROJECTION}（执行 --sync 生成）")
        return 1
    authority_sha = _sha256(AUTHORITY)
    projection_sha = _sha256(PROJECTION)
    if authority_sha != projection_sha:
        problems.append(
            f"投影副本与权威源不一致：权威源 {authority_sha[:16]}…，"
            f"投影 {projection_sha[:16]}…（以权威源为准，执行 --sync 覆写）"
        )
    if problems:
        for problem in problems:
            print(f"[FAIL] {problem}")
        return 1
    print(f"[PASS] 基线两份副本逐字节一致（{authority_sha[:16]}…）")
    return 0


def sync() -> int:
    """以冻结副本为权威源覆写投影副本（原始字节写入，不做换行翻译）。

    为什么用 write_bytes：Windows 上 ``write_text`` 默认把 ``\\n`` 翻译成
    ``\\r\\n``，会把权威源与投影的换行差异引入——改完哈希就对不上。
    """
    if not AUTHORITY.exists():
        print(f"[FAIL] 权威源缺失：{AUTHORITY}（先走修订流程补齐冻结副本）")
        return 1
    data = AUTHORITY.read_bytes()
    PROJECTION.write_bytes(data)
    print(f"[PASS] 已按权威源覆写投影副本：{PROJECTION}（{len(data)} 字节）")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="基线主文档权威源同步工具")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--check", action="store_true", help="校验两份一致（默认）")
    group.add_argument("--sync", action="store_true", help="以冻结副本覆写外部副本")
    args = parser.parse_args(argv)
    return sync() if args.sync else check()


if __name__ == "__main__":
    sys.exit(main())
