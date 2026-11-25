"""``[V-BLOCKED]`` 阻塞清单的唯一读取与校验入口。

用法::

    python scripts/verification_blocks.py                # 表格：全部条目与状态
    python scripts/verification_blocks.py --status blocked
    python scripts/verification_blocks.py --id V-BLOCK-TC-1
    python scripts/verification_blocks.py --check        # 判据；失败非零退出

为什么需要这个脚本而不是「直接看 YAML」
------------------------------------
一份清单最难防的不是写错字段，而是**变成第二个半真不真的叙述**：

* 引用了不存在的文档 → 读者以为有证据，实际打不开；
* 解除了一条却忘了写裁决 id → 下次有人依据它去动代码，却找不到谁批的；
* 裁决 id 写错一个字 → 台账里查不到，但 YAML 看上去完好；
* 状态与字段自相矛盾（BLOCKED 却带着 lifted_at）→ 两种读法都能自圆其说。

这四类问题都不会让「打开文件看一眼」变红。所以判据必须能独立运行，并且接进
``contract_check``——清单自己若是靠自觉维护的，它迟早会开始骗人。

判据的设计取向：**只做可机械判定的部分**。它不评价 `gap` 写得对不对、
`unblocks_when` 是否合理——那是复核者的事。它保证的是「这条记录指向的东西
真实存在，且状态与字段不矛盾」。
"""

from __future__ import annotations

import argparse
import datetime
import pathlib
import sys
from typing import Any

from packages.shared.yaml_strict import load_yaml_file

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
BLOCKS_PATH = REPO_ROOT / "contracts" / "phase0" / "verification_blocks.yaml"
AMENDMENTS_PATH = REPO_ROOT / "contracts" / "phase0" / "contract_amendments.yaml"

BLOCKED = "BLOCKED"
LIFTED = "LIFTED"
_STATUSES = frozenset({BLOCKED, LIFTED})

# 两类条目共用的必填字段。`gap` 对 LIFTED 也要求保留：一份「已解除」的记录
# 若不写当初缺什么，下一个人无法判断这次解除是否覆盖了他关心的事。
_REQUIRED_FIELDS = (
    "id",
    "title",
    "status",
    "raised_by",
    "raised_at",
    "evidence",
    "gap",
    "unblocks_when",
    "decision_owner",
)
_LIFT_FIELDS = ("lifted_at", "lifted_by", "lift_evidence")


def load_registry(path: pathlib.Path = BLOCKS_PATH) -> dict[str, Any]:
    """读清单（严格 YAML：重复键、非映射都会抛错，不静默取最后一个）。"""
    data = load_yaml_file(path)
    if not isinstance(data, dict):
        raise ValueError(f"{path} 顶层必须是映射")
    return data


def entries(registry: dict[str, Any]) -> list[dict[str, Any]]:
    raw = registry.get("blocks")
    if not isinstance(raw, list):
        raise ValueError("verification_blocks.yaml 缺少 blocks 列表")
    return [item for item in raw if isinstance(item, dict)]


def open_block_ids(registry: dict[str, Any]) -> tuple[str, ...]:
    """当前仍然阻塞的条目 id（按文件顺序）。"""
    return tuple(
        str(item.get("id"))
        for item in entries(registry)
        if str(item.get("status")) == BLOCKED
    )


def _amendment_ids(path: pathlib.Path) -> set[str]:
    data = load_yaml_file(path)
    amendments = data.get("amendments") if isinstance(data, dict) else None
    if not isinstance(amendments, list):
        raise ValueError(f"{path} 缺少 amendments 列表")
    return {
        str(item["id"])
        for item in amendments
        if isinstance(item, dict) and item.get("id")
    }


def _is_blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, dict)):
        return not value
    return False


def _path_problem(root: pathlib.Path, label: str, raw: Any) -> str | None:
    if not isinstance(raw, str) or not raw.strip():
        return f"{label} 必须是非空路径字符串，实际为 {raw!r}"
    if not (root / raw).is_file():
        return f"{label} 指向的文件不存在: {raw}"
    return None


def _date_problem(label: str, raw: Any) -> str | None:
    if not isinstance(raw, str) or not raw.strip():
        return f"{label} 必须是非空日期字符串"
    try:
        datetime.date.fromisoformat(raw)
    except ValueError:
        return f"{label} 不是合法 ISO 日期（YYYY-MM-DD）: {raw!r}"
    return None


def _lift_problems(
    item: dict[str, Any],
    *,
    root: pathlib.Path,
    amendment_ids: set[str],
) -> list[str]:
    """LIFTED 条目的交叉判据：裁决 id 必须真的存在，证据文档必须提到它。"""
    problems: list[str] = []
    missing = [field for field in _LIFT_FIELDS if _is_blank(item.get(field))]
    if missing:
        return [f"LIFTED 条目缺少字段: {missing}"]

    lifted_at = _date_problem("lifted_at", item.get("lifted_at"))
    if lifted_at:
        problems.append(lifted_at)

    lifted_by = str(item["lifted_by"])
    if lifted_by not in amendment_ids:
        problems.append(
            f"lifted_by 指向的台账修订不存在: {lifted_by}"
            "（解除必须有可核对的裁决，不能只在清单里说「已解除」）"
        )

    evidence = str(item["lift_evidence"])
    path_problem = _path_problem(root, "lift_evidence", evidence)
    if path_problem:
        problems.append(path_problem)
    else:
        text = (root / evidence).read_text(encoding="utf-8")
        if lifted_by not in text:
            problems.append(
                f"lift_evidence 文档 {evidence} 中找不到裁决 id {lifted_by}："
                "两处指针必须互为凭据，否则会出现「有裁决无记录」或「有记录无裁决」"
            )
    return problems


def check_registry(
    registry: dict[str, Any],
    *,
    root: pathlib.Path = REPO_ROOT,
    amendments_path: pathlib.Path = AMENDMENTS_PATH,
) -> list[str]:
    """返回全部问题（空列表表示通过）。不抛异常，让调用方一次看全。"""
    problems: list[str] = []
    items = entries(registry)
    if not items:
        return ["blocks 为空：一份空清单会让「没有阻塞」与「没检查」不可区分"]

    amendment_ids = _amendment_ids(amendments_path)

    seen: set[str] = set()
    for index, item in enumerate(items):
        block_id = str(item.get("id") or f"blocks[{index}]")
        if _is_blank(item.get("id")):
            problems.append(f"blocks[{index}] 缺少 id")
            continue
        if block_id in seen:
            problems.append(f"id 重复: {block_id}")
        seen.add(block_id)

        missing = [f for f in _REQUIRED_FIELDS if _is_blank(item.get(f))]
        if missing:
            problems.append(f"{block_id} 缺少字段: {missing}")
            continue

        status = str(item["status"])
        if status not in _STATUSES:
            problems.append(f"{block_id} 的 status 非法: {status!r}（应为 {sorted(_STATUSES)}）")
            continue

        for label in ("raised_by",):
            path_problem = _path_problem(root, f"{block_id}.{label}", item.get(label))
            if path_problem:
                problems.append(path_problem)
        for label in ("raised_at",):
            date_problem = _date_problem(f"{block_id}.{label}", item.get(label))
            if date_problem:
                problems.append(date_problem)

        evidence = item.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            problems.append(f"{block_id}.evidence 必须是非空列表")
        else:
            for raw in evidence:
                path_problem = _path_problem(root, f"{block_id}.evidence", raw)
                if path_problem:
                    problems.append(path_problem)

        present_lift = [f for f in _LIFT_FIELDS if not _is_blank(item.get(f))]
        if status == BLOCKED:
            if present_lift:
                problems.append(
                    f"{block_id} 状态为 BLOCKED 却带着解除字段 {present_lift}："
                    "状态与字段矛盾，两种读法都能自圆其说"
                )
        else:
            problems.extend(
                _lift_problems(item, root=root, amendment_ids=amendment_ids)
            )

    declared = registry.get("counts")
    if not isinstance(declared, dict):
        problems.append("缺少 counts 段（total / blocked / lifted）")
    else:
        actual = {
            "total": len(items),
            "blocked": sum(1 for i in items if str(i.get("status")) == BLOCKED),
            "lifted": sum(1 for i in items if str(i.get("status")) == LIFTED),
        }
        for key, value in actual.items():
            if declared.get(key) != value:
                problems.append(f"counts.{key} 声明 {declared.get(key)!r} 与实际 {value} 不符")

    return problems


def render_table(registry: dict[str, Any]) -> str:
    """人读视图：一行一条，解除的标注裁决 id。"""
    lines = [f"{'ID':<34} {'STATUS':<8} TITLE"]
    for item in entries(registry):
        status = str(item.get("status"))
        suffix = f"  ← lifted by {item.get('lifted_by')}" if status == LIFTED else ""
        lines.append(f"{item.get('id')!s:<34} {status:<8} {item.get('title')}{suffix}")
    counts = registry.get("counts") or {}
    lines.append(
        f"\n合计 {counts.get('total')} 条：blocked {counts.get('blocked')} / "
        f"lifted {counts.get('lifted')}"
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="[V-BLOCKED] 阻塞清单")
    parser.add_argument("--check", action="store_true", help="运行判据，失败非零退出")
    parser.add_argument(
        "--status",
        choices=["blocked", "lifted"],
        help="只列某一状态的条目",
    )
    parser.add_argument("--id", dest="block_id", help="只看一条")
    args = parser.parse_args(argv)

    registry = load_registry()

    if args.check:
        problems = check_registry(registry)
        if problems:
            for problem in problems:
                print(f"[FAIL] {problem}")
            return 1
        print(
            f"[PASS] 阻塞清单：{registry['counts']['total']} 条"
            f"（blocked {registry['counts']['blocked']} / lifted {registry['counts']['lifted']}）"
        )
        return 0

    selected = entries(registry)
    if args.status:
        wanted = BLOCKED if args.status == "blocked" else LIFTED
        selected = [i for i in selected if str(i.get("status")) == wanted]
    if args.block_id:
        selected = [i for i in selected if str(i.get("id")) == args.block_id]
        if not selected:
            print(f"未找到条目: {args.block_id}")
            return 1

    for item in selected:
        print(f"[{item.get('status')}] {item.get('id')} — {item.get('title')}")
        print(f"  出处: {item.get('raised_by')} ({item.get('raised_at')})")
        print(f"  缺口: {str(item.get('gap')).strip()}")
        print(f"  解除条件: {str(item.get('unblocks_when')).strip()}")
        print(f"  裁决人: {item.get('decision_owner')}")
        if str(item.get("status")) == LIFTED:
            print(f"  解除: {item.get('lifted_at')} / {item.get('lifted_by')}")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
