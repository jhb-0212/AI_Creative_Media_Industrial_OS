"""从当前迁移源码重建身份迁移语句清单（inventory）。

背景：2026-11-27 修复前生成的旧清单把 #24 的降级宽授、已被移除的
``CREATE/ALTER ROLE`` 语句都留在了归档里，且含明文开发口令。旧清单已移入
``docs/audits/superseded/`` 仅作历史证据，不再作为矩阵输入。

本脚本的生成策略：
1. 对每条 revision 的 ``upgrade`` / ``downgrade`` 用 Alembic offline 模式
   （``as_sql=True``）真实执行迁移函数，捕获它将要发出的 SQL——这是当前源码的事实；
2. offline 被 ``op.get_bind()`` 查询阻断时：
   - 该迁移自基线提交以来**未修改** → 沿用旧清单同 revision/direction 的语句，
     标记 ``CARRIED_FORWARD``（文件未变，语句等价）；
   - 已修改 → 退回 AST 静态抽取，标记 ``UNPROVEN``，不伪造覆盖；
3. 生成后统一擦除 ``PASSWORD '<...>'`` 与连接串口令；
4. 元数据记录 head SHA、生成时间、生成器、扫描范围、修改集与覆盖状态。

用法::

    python scripts/build_identity_inventory.py \
        --base-inventory docs/audits/superseded/2026-11-27-identity-migration-inventory.pre-regeneration.json \
        --out docs/audits/2026-11-27-identity-migration-inventory.json
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import io
import json
import re
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
_VERSIONS = _ROOT / "data" / "migrations" / "versions"
_BASE_COMMIT = "c4b9a52415258060f4310c24a245234b8b852511"

_PASSWORD_RE = re.compile(r"PASSWORD\s+'[^']*'", re.IGNORECASE)
_URL_SECRET_RE = re.compile(r"(postgres(?:ql)?://[^:\s/]+:)([^@\s]+)(@)", re.IGNORECASE)

CATEGORIES = {
    "create_role": re.compile(r"\bCREATE\s+ROLE\b", re.I),
    "alter_role": re.compile(r"\bALTER\s+ROLE\b", re.I),
    "drop_role": re.compile(r"\bDROP\s+ROLE\b", re.I),
    "grant": re.compile(r"\bGRANT\s+", re.I),
    "revoke": re.compile(r"\bREVOKE\s+", re.I),
    "default_privs": re.compile(r"\bALTER\s+DEFAULT\s+PRIVILEGES\b", re.I),
    "policy": re.compile(r"\b(CREATE|ALTER|DROP)\s+POLICY\b", re.I),
    "schema": re.compile(r"\b(CREATE|DROP)\s+SCHEMA\b", re.I),
    "owner": re.compile(r"\bOWNER\s+TO\b", re.I),
}


def _redact(sql: str) -> str:
    sql = _PASSWORD_RE.sub("PASSWORD '***REDACTED***'", sql)
    return _URL_SECRET_RE.sub(r"\1***REDACTED***\3", sql)


def _split_statements(text: str) -> list[str]:
    statements: list[str] = []
    buf: list[str] = []
    i = 0
    length = len(text)
    while i < length:
        ch = text[i]
        if ch == "$":
            match = re.match(r"\$[A-Za-z_0-9]*\$", text[i:])
            if match:
                tag = match.group(0)
                end = text.find(tag, i + len(tag))
                end = length if end < 0 else end + len(tag)
                buf.append(text[i:end])
                i = end
                continue
        if ch == "'":
            j = i + 1
            while j < length:
                if text[j] == "'" and (j + 1 >= length or text[j + 1] != "'"):
                    j += 1
                    break
                j += 2 if text[j] == "'" else 1
            buf.append(text[i:j])
            i = j
            continue
        if ch == ";":
            statements.append("".join(buf).strip())
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        statements.append(tail)
    return [s for s in statements if s]


def _offline_statements(module: Any, direction: str) -> tuple[list[str] | None, str | None]:
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    buf = io.StringIO()
    try:
        ctx = MigrationContext.configure(
            dialect_name="postgresql", opts={"as_sql": True, "output_buffer": buf}
        )
        with Operations.context(ctx):
            getattr(module, direction)()
    except Exception as exc:  # noqa: BLE001 - 需要记录任意失败原因
        return None, f"{type(exc).__name__}: {exc}"[:160]
    return _split_statements(buf.getvalue()), None


def _classify(sql: str) -> list[str]:
    return [name for name, pattern in CATEGORIES.items() if pattern.search(sql)]


def _roles(sql: str) -> list[str]:
    names = re.findall(r"(?:TO|FROM)\s+([A-Za-z_][A-Za-z0-9_]*)", sql, re.I)
    names += re.findall(r"DROP\s+ROLE\s+(?:IF\s+EXISTS\s+)?([A-Za-z_][A-Za-z0-9_]*)", sql, re.I)
    seen: list[str] = []
    for name in names:
        if name.upper() in {"PUBLIC", "TABLE", "SCHEMA", "ALL", "TABLES", "SEQUENCES"}:
            continue
        if name not in seen:
            seen.append(name)
    return seen


def _object(sql: str) -> str:
    match = re.search(
        r"ON\s+(?:ALL\s+[A-Z]+\s+IN\s+SCHEMA\s+)?([A-Za-z_][A-Za-z0-9_.\"]*)", sql, re.I
    )
    if match:
        return match.group(1).strip('"')
    match = re.search(r"ON\s+TABLE\s+([A-Za-z_][A-Za-z0-9_.\"]*)", sql, re.I)
    return match.group(1).strip('"') if match else ""


def _module_header(tree: ast.Module) -> tuple[str, str | None]:
    revision: str | None = None
    down: str | None = None
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            name, value = node.target.id, node.value
        elif isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name, value = node.targets[0].id, node.value
        else:
            continue
        if name not in {"revision", "down_revision"}:
            continue
        resolved = value.value if isinstance(value, ast.Constant) else None
        if name == "revision" and isinstance(resolved, str):
            revision = resolved
        elif name == "down_revision":
            down = resolved if isinstance(resolved, str) else None
    if revision is None:
        raise RuntimeError("缺少 revision 声明")
    return revision, down


def _chain(files: dict[str, tuple[str | None, Path]]) -> list[str]:
    by_parent: dict[str | None, str] = {parent: rev for rev, (parent, _) in files.items()}
    order: list[str] = []
    current = by_parent.get(None)
    while current is not None:
        order.append(current)
        current = next((rev for rev, (parent, _) in files.items() if parent == current), None)
    if len(order) != len(files):
        raise RuntimeError(f"迁移链不连通，未纳入：{sorted(set(files) - set(order))}")
    return order


def _modified_files() -> set[str]:
    out = subprocess.run(
        ["git", "diff", "--name-only", _BASE_COMMIT, "--", "data/migrations/versions"],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return {Path(line).name for line in out.splitlines() if line.strip()}


def build(base_inventory: Path) -> dict[str, Any]:
    files: dict[str, tuple[str | None, Path]] = {}
    trees: dict[str, ast.Module] = {}
    modules: dict[str, Any] = {}
    for path in sorted(_VERSIONS.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        rev, parent = _module_header(tree)
        files[rev] = (parent, path)
        trees[rev] = tree

    base = json.loads(base_inventory.read_text(encoding="utf-8"))
    base_rows = base.get("rows") or []
    modified = _modified_files()
    head_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=_ROOT, capture_output=True, text=True, check=True
    ).stdout.strip()

    rows: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    carried: list[dict[str, Any]] = []
    coverage: dict[str, str] = {}
    lifecycle: list[dict[str, Any]] = []
    per_revision: dict[str, Counter[str]] = {}
    totals: Counter[str] = Counter()

    for index, revision in enumerate(_chain(files)):
        parent, path = files[revision]
        if revision not in modules:
            spec = importlib.util.spec_from_file_location("inv_" + revision, path)
            module = importlib.util.module_from_spec(spec)
            try:
                spec.loader.exec_module(module)
            except Exception as exc:  # noqa: BLE001
                failures.append(
                    {"revision": revision, "direction": "import", "error": f"{type(exc).__name__}: {exc}"[:160], "unproven": True}
                )
                module = None
            modules[revision] = module
        module = modules[revision]
        status = "STATIC_COMPLETE"
        for direction in ("upgrade", "downgrade"):
            statements: list[str] = []
            source = "offline"
            reason: str | None = None
            if module is not None:
                statements_or_none, reason = _offline_statements(module, direction)
            else:
                statements_or_none, reason = None, "模块导入失败"
            if statements_or_none is not None:
                statements = statements_or_none
            else:
                carried_rows = [r for r in base_rows if r.get("revision") == revision and r.get("direction") == direction]
                if path.name not in modified and carried_rows:
                    statements = [str(r.get("sql") or "") for r in carried_rows]
                    source = "carried_forward"
                    carried.append({"revision": revision, "direction": direction, "rows": len(carried_rows)})
                else:
                    statements = []
                    failures.append(
                        {"revision": revision, "direction": direction, "error": reason or "offline 展开失败", "unproven": True}
                    )
                    status = "UNPROVEN"
            for sql in statements:
                sql = " ".join(sql.split())
                if not sql:
                    continue
                ops = _classify(sql)
                if not ops:
                    continue
                for op in ops:
                    totals[op] += 1
                per_revision.setdefault(revision, Counter()).update(ops)
                rows.append(
                    {
                        "revision": revision,
                        "file": path.name,
                        "chain_index": index,
                        "direction": direction,
                        "operation": ops[0],
                        "operations": ops,
                        "roles": _roles(sql),
                        "object": _object(sql),
                        "source": source,
                        "sql": _redact(sql)[:4000],
                    }
                )
                if any(op in {"create_role", "alter_role", "drop_role"} for op in ops):
                    lifecycle.append(
                        {
                            "revision": revision,
                            "chain_index": index,
                            "direction": direction,
                            "operation": ops[0],
                            "roles": _roles(sql),
                            "sql": _redact(sql)[:4000],
                            "class": f"{ops[0].upper()}_IN_MIGRATION",
                            "source": source,
                        }
                    )
        coverage[revision] = status

    return {
        "chain_length": len(files),
        "head": head_sha[:12],
        "head_sha": head_sha,
        "base_commit": _BASE_COMMIT,
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "generator": "scripts/build_identity_inventory.py（Alembic offline as_sql + AST 回退）",
        "scan_scope": "data/migrations/versions/*.py 全链 upgrade/downgrade 的 op.execute 语句",
        "redaction": "PASSWORD '<...>' 与连接串口令在归档前替换为 ***REDACTED***",
        "modified_revisions": sorted({rev for rev, (_, p) in files.items() if p.name in modified}),
        "carried_forward": carried,
        "statement_totals": dict(sorted(totals.items())),
        "per_revision": {rev: dict(sorted(c.items())) for rev, c in sorted(per_revision.items())},
        "lifecycle_flags": lifecycle,
        "expansion_failures": failures,
        "coverage": coverage,
        "rows": rows,
        "notes": {
            "carried_forward": "迁移自基线提交以来未修改，offline 被 get_bind 阻断时沿用旧清单同 revision/direction 的语句，文件等价。",
            "unproven": "已修改且 offline 展开失败的 direction 不沿用旧语句，标 UNPROVEN；其语句覆盖不完整。",
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="重建身份迁移语句清单（脱敏）")
    parser.add_argument("--base-inventory", required=True, help="旧清单路径（只读，用于未修改迁移的 carry-forward）")
    parser.add_argument("--out", required=True, help="输出 JSON 路径")
    args = parser.parse_args(argv)

    payload = build(Path(args.base_inventory))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "rows": len(payload["rows"]),
                "revisions": len(payload["per_revision"]),
                "expansion_failures": len(payload["expansion_failures"]),
                "unproven": [r for r, s in payload["coverage"].items() if s == "UNPROVEN"],
                "carried": len(payload["carried_forward"]),
                "out": str(out),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
