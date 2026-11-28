"""身份 DDL 盘点：迁移链里到底有多少「建角色 / 授权 / 改属主」的动作。

为什么要有它
------------
AUTHZ-7 的解除路径是「bootstrap 建身份，历史迁移只做结构」。在此之前必须先
知道要拆多少东西——而「大概二十多个迁移」这种口径不能用来派活：

- 有的迁移把 DDL 写成字面 SQL（``ALTER ROLE ... NOSUPERUSER``），
- 有的迁移不写字面 SQL，而是调用 ``db_grants.converge_statements`` 之类的
  生成器，字面扫描看不见它们。

两者都要数，否则拆完之后会剩下一半身份 DDL 藏在函数调用里，闸门还以为干净了。

判据（只读、不连库）
--------------------
对 ``data/migrations/versions/*.py`` 做 AST 解析，取出所有字符串常量，按 ``;``
切成语句片段，按首关键字归类：建角色、改角色、成员关系、授权、撤权、默认权限、
属主变更、策略、SET ROLE、扩展。另外统计「DDL 生成器」调用名。

``--check`` 与基线（``data/migrations/identity_ddl_baseline.json``）比对，任何
类别**只许减不许增**：重建迁移链期间，新迁移不得再引入身份 DDL，否则就是一边
拆一边漏。
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_VERSIONS_DIR = _REPO_ROOT / "data" / "migrations" / "versions"
_BASELINE_PATH = _REPO_ROOT / "data" / "migrations" / "identity_ddl_baseline.json"

EXIT_CLEAN = 0
EXIT_REGRESSION = 1

# 类别名同时是基线文件的键，改名等于让基线失效，因此保持稳定。
CATEGORY_ROLE_CREATE = "role_create"
CATEGORY_ROLE_ALTER = "role_alter"
CATEGORY_ROLE_DROP = "role_drop"
CATEGORY_GRANT = "grant"
CATEGORY_REVOKE = "revoke"
CATEGORY_DEFAULT_PRIVS = "default_privs"
CATEGORY_OWNER = "owner"
CATEGORY_POLICY = "policy"
CATEGORY_SET_ROLE = "set_role"
CATEGORY_EXTENSION = "extension"
CATEGORY_SCHEMA = "schema"

# 生成 DDL 的辅助函数名：「字面扫描看不见」的那一半。
_DDL_GENERATORS = frozenset(
    {
        "converge_statements",
        "infra_converge_statements",
        "maintenance_membership_statements",
        "maintenance_identity_statements",
        "scheduler_statements",
        "domain_grants_statements",
    }
)

_WHITESPACE = re.compile(r"\s+")
_EXECUTE_FORMAT = re.compile(
    r"\bEXECUTE\s+(?:format|quote_ident|quote_literal)\s*\(\s*'", re.IGNORECASE
)

# 语句在迁移里经常被包进 ``DO $$ BEGIN IF NOT EXISTS ... THEN <DDL> END IF; END $$``
# 或 ``EXECUTE format('<DDL>')``：片段未必以关键字开头，所以按「语句边界」找，
# 而不是只看首词。边界取分号切分后的段首、以及 PL/pgSQL 的分支关键字。
_BOUNDARY = r"(?:^|;\s*|\bTHEN\b|\bBEGIN\b|\bELSE\b|\bEXECUTE\b|\bLOOP\b)"
_PATTERNS: dict[str, re.compile[str]] = {
    CATEGORY_ROLE_CREATE: re.compile(rf"{_BOUNDARY}\s*CREATE\s+ROLE\b", re.IGNORECASE),
    CATEGORY_ROLE_ALTER: re.compile(rf"{_BOUNDARY}\s*ALTER\s+ROLE\b", re.IGNORECASE),
    # 销毁角色是身份生命周期的破坏性动作，必须单独计数：缺这一类时，
    # 「只许减不许增」的基线对 DROP ROLE 完全失明——升级真的在删角色，
    # 盘点却记 0，矩阵还能把它判成 symmetric（D4-10 实测复现）。
    CATEGORY_ROLE_DROP: re.compile(rf"{_BOUNDARY}\s*DROP\s+ROLE\b", re.IGNORECASE),
    CATEGORY_DEFAULT_PRIVS: re.compile(
        rf"{_BOUNDARY}\s*ALTER\s+DEFAULT\s+PRIVILEGES\b", re.IGNORECASE
    ),
    CATEGORY_GRANT: re.compile(rf"{_BOUNDARY}\s*GRANT\s+\S", re.IGNORECASE),
    CATEGORY_REVOKE: re.compile(rf"{_BOUNDARY}\s*REVOKE\s+\S", re.IGNORECASE),
    CATEGORY_POLICY: re.compile(rf"{_BOUNDARY}\s*(?:CREATE|ALTER|DROP)\s+POLICY\b", re.IGNORECASE),
    CATEGORY_SET_ROLE: re.compile(rf"{_BOUNDARY}\s*SET\s+(?:LOCAL\s+)?ROLE\b", re.IGNORECASE),
    CATEGORY_EXTENSION: re.compile(rf"{_BOUNDARY}\s*CREATE\s+EXTENSION\b", re.IGNORECASE),
    CATEGORY_SCHEMA: re.compile(rf"{_BOUNDARY}\s*CREATE\s+SCHEMA\b", re.IGNORECASE),
    CATEGORY_OWNER: re.compile(r"\bOWNER\s+TO\b", re.IGNORECASE),
}


@dataclass(frozen=True, slots=True)
class MigrationAudit:
    """一个迁移文件的盘点结果。"""

    filename: str
    categories: dict[str, int] = field(default_factory=dict)
    generators: dict[str, int] = field(default_factory=dict)

    @property
    def identity_actions(self) -> int:
        return sum(self.categories.values()) + sum(self.generators.values())


def _normalize(sql: str) -> str:
    return _WHITESPACE.sub(" ", sql).strip()


def classify_sql(sql: str) -> Counter[str]:
    """把一段 SQL 切成语句并归类。纯函数，供单测直接喂样本。

    一个片段可能命中多个类别（``REVOKE x; GRANT y`` 挤在同一段、
    ``DO $$ ... CREATE ROLE ... ``），所以各类独立计数，不做互斥分支。
    """
    counts: Counter[str] = Counter()
    # ``EXECUTE format('GRANT %I TO ...')`` 中间隔了 format( 与引号，关键字不再
    # 紧跟边界；先把这层壳拆掉，让里面的语句回到段首位置。
    normalized = _EXECUTE_FORMAT.sub(" ; ", _normalize(sql))
    for category, pattern in _PATTERNS.items():
        hits = len(pattern.findall(normalized))
        if hits:
            counts[category] += hits
    return counts


def audit_file(path: Path) -> MigrationAudit:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    categories: Counter[str] = Counter()
    generators: Counter[str] = Counter()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in _DDL_GENERATORS:
                generators[node.func.id] += 1
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in _DDL_GENERATORS:
                generators[node.func.attr] += 1
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            categories.update(classify_sql(node.value))
    return MigrationAudit(
        filename=path.name,
        categories=dict(sorted(categories.items())),
        generators=dict(sorted(generators.items())),
    )


def audit_tree(versions_dir: Path = _VERSIONS_DIR) -> list[MigrationAudit]:
    audits = [audit_file(path) for path in sorted(versions_dir.glob("*.py"))]
    return [item for item in audits if item.identity_actions > 0]


def _totals(audits: list[MigrationAudit]) -> dict[str, int]:
    total: Counter[str] = Counter()
    for item in audits:
        total.update(item.categories)
        total.update(item.generators)
    return dict(sorted(total.items()))


def _report(audits: list[MigrationAudit]) -> dict[str, object]:
    return {
        "migrations_with_identity_ddl": len(audits),
        "total_actions": sum(item.identity_actions for item in audits),
        "totals": _totals(audits),
        "per_migration": {
            item.filename: {
                "actions": item.identity_actions,
                **item.categories,
                **{f"gen:{name}": v for name, v in item.generators.items()},
            }
            for item in sorted(audits, key=lambda a: a.identity_actions, reverse=True)
        },
    }


def _check_regression(totals: dict[str, int]) -> list[str]:
    if not _BASELINE_PATH.exists():
        return [f"基线文件不存在：{_BASELINE_PATH.relative_to(_REPO_ROOT)}"]
    baseline: dict[str, int] = json.loads(_BASELINE_PATH.read_text(encoding="utf-8"))
    problems: list[str] = []
    for key, expected in sorted(baseline.items()):
        actual = totals.get(key, 0)
        if actual > expected:
            problems.append(f"{key}: {actual} > 基线 {expected}（不得新增身份 DDL）")
    return problems


def write_baseline(audits: list[MigrationAudit]) -> Path:
    _BASELINE_PATH.write_text(
        json.dumps(_totals(audits), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return _BASELINE_PATH


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="迁移链身份 DDL 盘点（只读）")
    parser.add_argument("--check", action="store_true", help="与基线比对，只许减不许增")
    parser.add_argument(
        "--write-baseline",
        action="store_true",
        help="重写基线（仅在真正拆除身份 DDL 的提交里使用）",
    )
    args = parser.parse_args(argv)

    audits = audit_tree()
    print(json.dumps(_report(audits), ensure_ascii=False, indent=2))

    if args.write_baseline:
        print(f"[BASELINE] 写入 {write_baseline(audits)}")
        return EXIT_CLEAN

    if args.check:
        problems = _check_regression(_totals(audits))
        for problem in problems:
            print(f"[REGRESSION] {problem}", file=sys.stderr)
        if problems:
            return EXIT_REGRESSION
        print("[PASS] 身份 DDL 未超出基线（只减不增）")
    return EXIT_CLEAN


if __name__ == "__main__":
    raise SystemExit(main())
