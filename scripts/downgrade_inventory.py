"""生成「数据约束型降级」的 revision 清单（结构事实 + 初判类别）。

用途：ADR-R46 §4 要求每条 revision 至少列出升降级动作、影响对象、数据前置条件、
是否允许自动降级。本脚本只负责**把事实从迁移源码里抽出来**（AST 解析 `op.*`
调用），类别按 ADR 的规则给出**初判**，并在表里明确标注"待签字"——脚本不替人
做归类结论。

用法::

    python scripts/downgrade_inventory.py            # 打印 markdown 表
    python scripts/downgrade_inventory.py --json PATH # 同时落 JSON
"""

from __future__ import annotations

import argparse
import ast
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_VERSIONS = _ROOT / "data" / "migrations" / "versions"

_REVISION_RE = re.compile(r"^revision:\s*str\s*=\s*[\"']([0-9a-f]{12})[\"']", re.M)
_DOWN_REVISION_RE = re.compile(
    r"^down_revision:\s*(?:str\s*\|\s*None\s*)?=\s*(?:[\"']([0-9a-f]{12})[\"']|None)", re.M
)

#: 已有人工结论的 revision：类别按实证，不按启发式。
_KNOWN: dict[str, tuple[str, str, str]] = {
    # revision: (类别, 是否允许自动降级, 依据)
    "e1f2a3b4c5d6": (
        "B 数据前置条件",
        "guarded",
        "ck_generation_tasks_status 的 VALIDATE 在 `AWAITING_ASSET_REGISTRATION` 在途行存在时失败（实测）",
    ),
    "f9a0b1c2d3e4": (
        "A 无数据要求",
        "auto",
        "D-02 修复后降级只 DROP TABLE，索引随表删除（实测往返通过）",
    ),
    "a0b1c2d3e4f5": (
        "A 无数据要求",
        "auto",
        "R16 cutover 结构搬迁，降级重建旧表；D-02 组合往返实测通过",
    ),
    "c1a2b3d4e5f6": (
        "A 无数据要求",
        "auto",
        "D-07：降级按冻结范围回滚授权，只碰自己 upgrade 动过的表",
    ),
}

_DDL_LOSSY = {"drop_column", "drop_table", "drop_constraint", "alter_column"}
_DDL_ADD = {"create_table", "add_column", "create_index", "create_unique_constraint"}
_DATA_HINTS = ("VALIDATE", "RAISE EXCEPTION", "NOT VALID")


@dataclass
class RevisionFacts:
    revision: str
    file: str
    down_revision: str | None
    summary: str
    upgrade_ops: list[str] = field(default_factory=list)
    downgrade_ops: list[str] = field(default_factory=list)
    upgrade_tables: list[str] = field(default_factory=list)
    downgrade_tables: list[str] = field(default_factory=list)
    data_hints: list[str] = field(default_factory=list)
    has_downgrade: bool = False


def _docstring_summary(source: str) -> str:
    match = re.search(r'"""(.+)', source, re.S)
    if not match:
        return ""
    first = match.group(1).strip().splitlines()[0]
    return first.strip().strip('"').strip()


_ALEMBIC_OPS = frozenset(
    {
        "execute",
        "bulk_insert",
        "create_table",
        "drop_table",
        "rename_table",
        "create_index",
        "drop_index",
        "add_column",
        "drop_column",
        "alter_column",
        "create_unique_constraint",
        "create_check_constraint",
        "create_foreign_key",
        "create_primary_key",
        "create_exclude_constraint",
        "drop_constraint",
        "create_sequence",
        "drop_sequence",
        "create_table_comment",
        "drop_table_comment",
        "batch_alter_table",
        "f",
    }
)
_TABLE_ARG_OPS = frozenset(
    {
        "create_table",
        "drop_table",
        "create_index",
        "drop_index",
        "create_unique_constraint",
        "create_check_constraint",
        "create_foreign_key",
        "create_primary_key",
        "create_exclude_constraint",
        "drop_constraint",
        "add_column",
        "drop_column",
    }
)


def _collect(node: ast.AST, facts: RevisionFacts, direction: str) -> None:
    ops = facts.upgrade_ops if direction == "upgrade" else facts.downgrade_ops
    tables = facts.upgrade_tables if direction == "upgrade" else facts.downgrade_tables
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call):
            continue
        func = sub.func
        name = ""
        if (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "op"
        ):
            name = func.attr
        if name not in _ALEMBIC_OPS or name == "f":
            continue
        if name == "execute":
            literals = [
                a.value
                for a in sub.args
                if isinstance(a, ast.Constant) and isinstance(a.value, str)
            ]
            text = "\n".join(literals)
            for hint in _DATA_HINTS:
                if hint in text:
                    facts.data_hints.append(f"{direction}:{hint}")
            for table in re.findall(
                r"(?:FROM|INTO|ON|TABLE|UPDATE)\s+([a-z_]+(?:\.[a-z_]+)?)", text
            ):
                if table not in tables:
                    tables.append(table)
            ops.append("execute")
            continue
        ops.append(name)
        if (
            name in _TABLE_ARG_OPS
            and sub.args
            and isinstance(sub.args[0], ast.Constant)
            and isinstance(sub.args[0].value, str)
        ):
            value = str(sub.args[0].value)
            if value not in tables:
                tables.append(value)


def _classify(facts: RevisionFacts) -> tuple[str, str, str]:
    if facts.revision in _KNOWN:
        cls, auto, basis = _KNOWN[facts.revision]
        return cls, auto, basis
    if facts.data_hints:
        return (
            "B 数据前置条件",
            "guarded",
            "降级 SQL 含 VALIDATE/RAISE EXCEPTION，需先过守卫（初判）",
        )
    downgrade_ddl = {op for op in facts.downgrade_ops if op in _DDL_LOSSY}
    if downgrade_ddl:
        return (
            "E 有损数据变更",
            "manual",
            f"降级含 {sorted(downgrade_ddl)}：升级后写入的数据会丢失（初判，待签字）",
        )
    if not facts.has_downgrade:
        return ("F 不可逆变更", "unsupported", "没有 downgrade 实现（初判，待签字）")
    if not facts.downgrade_ops:
        return (
            "A 无数据要求",
            "auto",
            "降级委托共享 helper（语句不在本文件），需人工核对 helper 是否带数据守卫",
        )
    return ("A 无数据要求", "auto", "只有授权/策略/索引类动作（初判，待签字）")


def _module_value(tree: ast.Module, name: str) -> str | None:
    for node in tree.body:
        target: str | None = None
        value: ast.expr | None = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target_node = node.targets[0]
            if isinstance(target_node, ast.Name):
                target = target_node.id
                value = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target = node.target.id
            value = node.value
        if target != name or value is None:
            continue
        if isinstance(value, ast.Constant):
            return str(value.value) if value.value is not None else None
    return None


def _chain_from_alembic() -> list[str] | None:
    """用 Alembic 自己的 ScriptDirectory 取链序（避免手写 down_revision 映射出错）。"""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    for name in ("alembic.ini", "data/migrations/alembic.ini"):
        candidate = _ROOT / name
        if candidate.exists():
            script = ScriptDirectory.from_config(Config(str(candidate)))
            revisions = [rev.revision for rev in script.walk_revisions()]
            return list(reversed(revisions))
    return None


def _load() -> list[RevisionFacts]:
    facts: dict[str, RevisionFacts] = {}
    for path in sorted(_VERSIONS.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        revision = _module_value(tree, "revision")
        if not revision:
            continue
        item = RevisionFacts(
            revision=revision,
            file=path.name,
            down_revision=_module_value(tree, "down_revision"),
            summary=_docstring_summary(source),
        )
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name in {"upgrade", "downgrade"}:
                if node.name == "downgrade":
                    item.has_downgrade = True
                _collect(node, item, node.name)
        facts[item.revision] = item
    return list(facts.values())


def _chain_order(facts: list[RevisionFacts]) -> list[RevisionFacts]:
    order = _chain_from_alembic()
    if order:
        by_rev = {item.revision: item for item in facts}
        resolved = [by_rev[rev] for rev in order if rev in by_rev]
        rest = [item for item in facts if item.revision not in set(order)]
        return resolved + rest
    by_down: dict[str | None, RevisionFacts] = {item.down_revision: item for item in facts}
    fallback: list[RevisionFacts] = []
    cursor: str | None = None
    while cursor in by_down:
        item = by_down[cursor]
        fallback.append(item)
        cursor = item.revision
    main = {item.revision for item in fallback}
    return fallback + [item for item in facts if item.revision not in main]


@dataclass
class Row:
    chain_index: int
    revision: str
    file: str
    summary: str
    upgrade_ops: list[str]
    downgrade_ops: list[str]
    objects: list[str]
    data_class: str
    auto_downgrade: str
    basis: str
    data_hints: list[str]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成降级 revision 清单（事实 + 初判）")
    parser.add_argument("--json", metavar="PATH", help="同时输出 JSON")
    args = parser.parse_args(argv)

    chain = _chain_order(_load())
    rows: list[Row] = []
    for index, item in enumerate(chain, start=0):
        cls, auto, basis = _classify(item)
        rows.append(
            Row(
                chain_index=index,
                revision=item.revision,
                file=item.file,
                summary=item.summary,
                upgrade_ops=item.upgrade_ops,
                downgrade_ops=item.downgrade_ops,
                objects=item.upgrade_tables + item.downgrade_tables,
                data_class=cls,
                auto_downgrade=auto,
                basis=basis,
                data_hints=item.data_hints,
            )
        )

    print("| # | revision | 升级动作 | 降级动作 | 影响对象 | 类别 | 自动降级 | 依据 |")
    print("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for row in rows:
        up = ", ".join(row.upgrade_ops[:6]) or "—"
        down = ", ".join(row.downgrade_ops[:6]) or "—"
        objs = ", ".join(row.objects[:4]) or "—"
        print(
            f"| {row.chain_index} | `{row.revision}` | {up} | {down} | {objs} | "
            f"{row.data_class} | {row.auto_downgrade} | {row.basis} |"
        )

    if args.json:
        path = Path(args.json)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {"generated": True, "rows": [asdict(row) for row in rows]},
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"[JSON] {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
