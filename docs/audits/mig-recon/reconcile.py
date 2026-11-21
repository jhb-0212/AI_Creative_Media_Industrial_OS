"""逐 revision 结构对账：把「迁移声明」与「库内实际对象」对上。

方法（与「看 alembic_version 猜」的区别）
------------------------------------------
``alembic_version`` 只有一行 head，且同一 ID 在两支可能是不同实现（本项目实测 10 处）。
因此「某 revision 是否真的应用过」必须由**结构标记**证明：

1. 从固定提交的迁移源码里抽取它创建的对象（schema/table/column/index/constraint/
   trigger/function/policy）；
2. 扣掉被后续迁移 drop 掉的标记（否则「已应用」会被判成「缺失」）；
3. 在每个库里逐标记核对存在性，得到「全部命中 / 部分命中 / 零命中」。

局限（必须写进报告，不能装作没有）
--------------------------------
- 动态拼名的 DDL（循环建分区、f-string 表名）抽不出静态标记 → 该 revision 标记为空，
  判为「无静态标记」，不据此下结论；
- 约束/索引在后续迁移里被重建时，名字可能变化 → 以名字命中为准，名字变即视为缺失，
  这类情况单独列为「需人工确认」，而不是直接判未应用。
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

VERSIONS_DIR = "data/migrations/versions"
REVISION_RE = re.compile(r"^revision(?:\s*:\s*str)?\s*=\s*[\"']([^\"']+)[\"']", re.MULTILINE)
DOWN_RE = re.compile(r"^down_revision(?:\s*:[^=]+)?\s*=\s*(.+)$", re.MULTILINE)

# ---- 源码抽取规则 --------------------------------------------------------- #
CREATE_TABLE_RE = re.compile(r"op\.create_table\(\s*[\"'](\w+)[\"']")
ADD_COLUMN_RE = re.compile(r"op\.add_column\(\s*[\"'](\w+)[\"']\s*,\s*sa\.Column\(\s*[\"'](\w+)[\"']")
CREATE_INDEX_RE = re.compile(r"op\.create_index\(\s*[\"'](\w+)[\"']")
C_FK_RE = re.compile(r"op\.create_foreign_key\(\s*(?:[\"'](\w+)[\"']|None)")
C_CHECK_RE = re.compile(r"op\.create_check_constraint\(\s*[\"'](\w+)[\"']")
C_UNIQUE_RE = re.compile(r"op\.create_unique_constraint\(\s*[\"'](\w+)[\"']")
SCHEMA_KW_RE = re.compile(r"schema\s*=\s*[\"'](\w+)[\"']")

SQL_SCHEMA_RE = re.compile(
    r"CREATE\s+SCHEMA\s+(?:IF\s+NOT\s+EXISTS\s+)?[\"']?([A-Za-z_][A-Za-z0-9_]*)[\"']?", re.I
)
SQL_TABLE_RE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_][\w]*\.)?([A-Za-z_][\w]*)", re.I
)
SQL_POLICY_RE = re.compile(r"CREATE\s+POLICY\s+[\"']?([A-Za-z_][\w]*)[\"']?", re.I)
SQL_TRIGGER_RE = re.compile(r"CREATE\s+TRIGGER\s+[\"']?([A-Za-z_][\w]*)[\"']?", re.I)
SQL_FUNCTION_RE = re.compile(r"CREATE\s+OR\s+REPLACE\s+FUNCTION\s+([A-Za-z_][\w.]*)", re.I)
SQL_ADD_COLUMN_RE = re.compile(
    r"ALTER\s+TABLE\s+(?:ONLY\s+)?([A-Za-z_][\w]*\.)?([A-Za-z_][\w]*)\s+"
    r"ADD\s+COLUMN\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_][\w]*)",
    re.I,
)
SQL_ADD_CONSTRAINT_RE = re.compile(r"ADD\s+CONSTRAINT\s+([A-Za-z_][\w]*)", re.I)
SQL_ENABLE_RLS_RE = re.compile(
    r"ALTER\s+TABLE\s+(?:ONLY\s+)?([A-Za-z_][\w]*\.)?([A-Za-z_][\w]*)\s+"
    r"ENABLE\s+ROW\s+LEVEL\s+SECURITY",
    re.I,
)
SQL_FORCE_RLS_RE = re.compile(
    r"ALTER\s+TABLE\s+(?:ONLY\s+)?([A-Za-z_][\w]*\.)?([A-Za-z_][\w]*)\s+"
    r"FORCE\s+ROW\s+LEVEL\s+SECURITY",
    re.I,
)

DROP_TABLE_RE = re.compile(
    r"(?:op\.drop_table\(\s*[\"']|DROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?)([A-Za-z_][\w\"]*)", re.I
)
DROP_COLUMN_RE = re.compile(
    r"(?:op\.drop_column\(\s*[\"']([\w]+)[\"']|DROP\s+COLUMN\s+(?:IF\s+EXISTS\s+)?([A-Za-z_][\w]*))",
    re.I,
)
DROP_CONSTRAINT_RE = re.compile(r"op\.drop_constraint\(\s*[\"']([\w]+)[\"']")
DROP_POLICY_RE = re.compile(r"DROP\s+POLICY\s+(?:IF\s+EXISTS\s+)?([A-Za-z_][\w]*)", re.I)
DROP_TRIGGER_RE = re.compile(r"DROP\s+TRIGGER\s+(?:IF\s+EXISTS\s+)?([A-Za-z_][\w]*)", re.I)
DROP_FUNCTION_RE = re.compile(r"DROP\s+FUNCTION\s+(?:IF\s+EXISTS\s+)?([A-Za-z_][\w.]*)", re.I)

KIND_ORDER = (
    "schema",
    "table",
    "column",
    "index",
    "constraint",
    "trigger",
    "function",
    "policy",
    "rls",
    "rls_force",
)


@dataclass
class Revision:
    ref: str
    path: str
    revision: str
    down_revision: str | None
    sha256: str
    source: str
    markers: set[tuple[str, ...]] = field(default_factory=set)
    drops: set[tuple[str, ...]] = field(default_factory=set)

    @property
    def filename(self) -> str:
        return Path(self.path).name


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, encoding="utf-8"
    ).stdout


def _parse_down(raw: str) -> str | None:
    value = raw.strip()
    if value in {"None", "()", "[]"}:
        return None
    ids = re.findall(r"[\"']([^\"']+)[\"']", value)
    if len(ids) != 1:
        raise ValueError(f"不支持多父 down_revision：{value}")
    return ids[0]


def load(repo: Path, ref: str) -> list[Revision]:
    files = [
        line
        for line in _git(repo, "ls-tree", "-r", "--name-only", ref, "--", VERSIONS_DIR).splitlines()
        if line.endswith(".py") and "__init__" not in line
    ]
    result: list[Revision] = []
    for path in sorted(files):
        source = _git(repo, "show", f"{ref}:{path}")
        revision = REVISION_RE.search(source)
        down = DOWN_RE.search(source)
        if revision is None or down is None:
            continue
        item = Revision(
            ref=ref,
            path=path,
            revision=revision.group(1),
            down_revision=_parse_down(down.group(1)),
            sha256=hashlib.sha256(source.encode("utf-8")).hexdigest(),
            source=source,
        )
        _extract(item)
        result.append(item)
    return result


CONST_RE = re.compile(r"^([A-Z][A-Z0-9_]*)\s*(?::\s*str\s*)?=\s*[\"']([^\"']+)[\"']", re.MULTILINE)


def _resolve_constants(source: str) -> str:
    """把 f-string 里的模块级常量占位替换成字面值。

    ``CREATE TABLE {SCHEMA}.{TABLE}`` 在源码里没有任何可抽取的名字；但
    ``SCHEMA = "audit"`` 与 ``TABLE = "status_transitions"`` 是模块级常量，
    替换后即可得到 ``CREATE TABLE audit.status_transitions``。不做这一步，
    「对象是动态拼的」就会被误读成「对象不存在」。
    """
    for name, value in CONST_RE.findall(source):
        source = source.replace("{" + name + "}", value)
    return source


def _extract(item: Revision) -> None:
    source = _resolve_constants(item.source)
    for match in CREATE_TABLE_RE.finditer(source):
        tail = source[match.end() : match.end() + 400]
        schema = SCHEMA_KW_RE.search(tail)
        item.markers.add(("table", schema.group(1) if schema else "*", match.group(1)))
    for match in ADD_COLUMN_RE.finditer(source):
        item.markers.add(("column", match.group(1), match.group(2)))
    for match in CREATE_INDEX_RE.finditer(source):
        item.markers.add(("index", match.group(1)))
    for match in C_FK_RE.finditer(source):
        if match.group(1):
            item.markers.add(("constraint", match.group(1)))
    for pattern in (C_CHECK_RE, C_UNIQUE_RE):
        for match in pattern.finditer(source):
            item.markers.add(("constraint", match.group(1)))
    for match in SQL_SCHEMA_RE.finditer(source):
        # ``CREATE SCHEMA IF NOT EXISTS {schema}``（f-string 拼名）只能取到 "IF"，
        # 这类占位不是对象名，跳过而不是记成一个必然缺失的标记。
        if match.group(1).upper() not in {"IF", "EXISTS", "NOT"}:
            item.markers.add(("schema", match.group(1)))
    for match in SQL_TABLE_RE.finditer(source):
        schema = (match.group(1) or "").strip('".')
        item.markers.add(("table", schema or "*", match.group(2).strip('"')))
    for match in SQL_POLICY_RE.finditer(source):
        # 名字以 ``_`` 结尾说明是 f-string 拼的（``tenant_isolation_{table}``）：
        # 前缀匹配会把「别的表上的同名策略」当成证据，宁可不记标记。
        if not match.group(1).endswith("_"):
            item.markers.add(("policy", match.group(1)))
    for match in SQL_TRIGGER_RE.finditer(source):
        if not match.group(1).endswith("_"):
            item.markers.add(("trigger", match.group(1)))
    for match in SQL_FUNCTION_RE.finditer(source):
        item.markers.add(("function", match.group(1)))
    for match in SQL_ADD_COLUMN_RE.finditer(source):
        item.markers.add(("column", match.group(2).strip('"'), match.group(3).strip('"')))
    for match in SQL_ADD_CONSTRAINT_RE.finditer(source):
        item.markers.add(("constraint", match.group(1)))
    for match in SQL_ENABLE_RLS_RE.finditer(source):
        schema = (match.group(1) or "").strip('".')
        item.markers.add(("rls", schema or "*", match.group(2).strip('"')))
    for match in SQL_FORCE_RLS_RE.finditer(source):
        schema = (match.group(1) or "").strip('".')
        item.markers.add(("rls_force", schema or "*", match.group(2).strip('"')))

    for match in DROP_TABLE_RE.finditer(source):
        item.drops.add(("table", match.group(1).strip('"')))
    for match in DROP_COLUMN_RE.finditer(source):
        name = match.group(2) or match.group(1)
        if name:
            item.drops.add(("column", name.strip('"')))
    for match in DROP_CONSTRAINT_RE.finditer(source):
        item.drops.add(("constraint", match.group(1)))
    for match in DROP_POLICY_RE.finditer(source):
        item.drops.add(("policy", match.group(1)))
    for match in DROP_TRIGGER_RE.finditer(source):
        item.drops.add(("trigger", match.group(1)))
    for match in DROP_FUNCTION_RE.finditer(source):
        item.drops.add(("function", match.group(1)))


def _dynamic_aware(kind: str, name: str) -> tuple[str, ...]:
    """f-string 拼出的名字（``tenant_isolation_{table}``）只能取到前缀。

    截断到 ``_`` 结尾即视为「前缀标记」，按前缀匹配——否则会把
    「名字被动态拼接」误判成「对象缺失」，那是最典型的假阳性来源。
    """
    return (f"{kind}_prefix", name) if name.endswith("_") else (kind, name)


def head_and_chain(items: list[Revision], head: str) -> list[Revision]:
    by_id = {item.revision: item for item in items}
    order: list[str] = []
    current: str | None = head
    while current:
        order.append(current)
        current = by_id[current].down_revision
    return [by_id[revision] for revision in reversed(order)]


# --------------------------------------------------------------------------- #
# 目录导出解析
# --------------------------------------------------------------------------- #
def parse_dump(path: Path) -> dict[str, set[tuple[str, ...]]]:
    """把 q_catalog.sql 的输出解析成「每个库的对象集合」。"""
    databases: dict[str, set[tuple[str, ...]]] = {}
    current: str | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("########## DATABASE "):
            current = line.split()[2]
            databases[current] = set()
            continue
        if current is None or "|" not in line:
            continue
        parts = line.split("|")
        kind = parts[0]
        if kind == "schema":
            databases[current].add(("schema", parts[1]))
        elif kind == "table":
            databases[current].add(("table", parts[1], parts[2]))
            if parts[5] == "t":
                databases[current].add(("rls", parts[1], parts[2]))
            if parts[6] == "t":
                databases[current].add(("rls_force", parts[1], parts[2]))
        elif kind == "column":
            databases[current].add(("column", parts[2], parts[3]))
        elif kind == "constraint":
            databases[current].add(("constraint", parts[3]))
        elif kind == "index":
            databases[current].add(("index", parts[3]))
        elif kind == "trigger":
            databases[current].add(("trigger", parts[3]))
        elif kind == "function":
            databases[current].add(("function", f"{parts[1]}.{parts[2]}"))
        elif kind == "policy":
            databases[current].add(("policy", parts[3]))
    return databases


def matches(marker: tuple[str, ...], objects: set[tuple[str, ...]]) -> bool:
    if marker in objects:
        return True
    # 未写 schema 的标记（迁移里用默认 search_path）按名字匹配任意 schema。
    if marker[0] in {"table", "rls", "rls_force"} and marker[1] == "*":
        return any(obj[0] == marker[0] and obj[2] == marker[2] for obj in objects)
    if marker[0].endswith("_prefix"):
        real_kind = marker[0][: -len("_prefix")]
        return any(
            obj[0] == real_kind and obj[1].startswith(marker[1]) for obj in objects
        )
    # 函数未限定 schema 时，dump 里存的是 schema.name，按后缀匹配。
    if marker[0] == "function" and "." not in marker[1]:
        return any(obj[0] == "function" and obj[1].endswith(f".{marker[1]}") for obj in objects)
    return False


def main() -> int:
    repo = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    dump_path = Path(sys.argv[2] if len(sys.argv) > 2 else "docs/audits/mig-recon/catalog-dump.txt")
    refs = sys.argv[3:] or ["main", "origin/feat/identity-data-ownership"]
    databases = parse_dump(dump_path)

    left = load(repo, refs[0])
    right = load(repo, refs[1])
    left_chain = head_and_chain(left, sorted({r.revision for r in left} - {r.down_revision for r in left})[0])
    right_chain = head_and_chain(
        right, sorted({r.revision for r in right} - {r.down_revision for r in right})[0]
    )

    dropped: set[tuple[str, ...]] = set()
    for item in left_chain:
        dropped |= item.drops

    print("## 主支链逐 revision 结构核对\n")
    header = "| # | revision | 文件名 | 静态标记 | " + " | ".join(sorted(databases)) + " |"
    print(header)
    print("| --- | --- | --- | --- | " + " | ".join("---" for _ in databases) + " |")
    for index, item in enumerate(left_chain, start=1):
        live = {m for m in item.markers if not _is_dropped(m, dropped)}
        cells: list[str] = []
        for name in sorted(databases):
            objects = databases[name]
            hit = sum(1 for marker in live if matches(marker, objects))
            if not live:
                cells.append("无静态标记")
            elif hit == len(live):
                cells.append(f"命中 {hit}/{len(live)}")
            elif hit == 0:
                cells.append(f"**0/{len(live)}**")
            else:
                cells.append(f"**部分 {hit}/{len(live)}**")
        print(f"| {index} | `{item.revision}` | {item.filename} | {len(live)} | " + " | ".join(cells) + " |")

    print("\n## 功能分支独有 revision（主支链中不存在）\n")
    left_ids = {item.revision for item in left}
    print("| # | revision | 文件名 | 静态标记 | " + " | ".join(sorted(databases)) + " |")
    print("| --- | --- | --- | --- | " + " | ".join("---" for _ in databases) + " |")
    for index, item in enumerate([r for r in right_chain if r.revision not in left_ids], start=1):
        cells = []
        for name in sorted(databases):
            hit = sum(1 for marker in item.markers if matches(marker, databases[name]))
            cells.append(f"{hit}/{len(item.markers)}" if item.markers else "无静态标记")
        print(f"| {index} | `{item.revision}` | {item.filename} | {len(item.markers)} | " + " | ".join(cells) + " |")
    return 0


def _is_dropped(marker: tuple[str, ...], dropped: set[tuple[str, ...]]) -> bool:
    if marker in dropped:
        return True
    # 只按名字匹配的 drop（table/column 只取末段名字）
    if marker[0] in {"table", "column"}:
        return (marker[0], marker[-1]) in dropped
    return (marker[0], marker[1]) in dropped


if __name__ == "__main__":
    raise SystemExit(main())
