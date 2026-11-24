"""TECH-01 PoC 一次性取证脚本：规范化对比 PG16 / PG18 catalog 快照。

判据（可复算、无人工清单）：
- 按 probe.sql 的 11 段标题切段；
- 每行按 | 切列、strip，规范化空白与表头分隔线；
- 剔除 PG18 合法版本差异：
  1) pg_constraint contype='n'（NOT NULL 约束 PG18 才进 catalog）；
  2) owner ACL 里的 'm'（PG18 MAINTAIN 权限）与 'Dxtm'/'Dxt' 无关差异——
     仅删除 grantor=postgres 的 owner 条目中的 m，业务角色条目不动；
- 逐段用「规范化后的行集合」对比，输出 仅PG16 / 仅PG18 的行。
  业务实体集合应完全相等；不等即回退（FAIL）。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def parse(path: Path) -> dict[str, list[str]]:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    sections: dict[str, list[str]] = {}
    current = "PREAMBLE"
    for raw in lines:
        m = re.match(r"^=== (\d+)\. ", raw)
        if m:
            current = m.group(1)
            sections.setdefault(current, [])
            continue
        sections.setdefault(current, [])
        line = raw.strip()
        if not line:
            continue
        if set(line) <= set("-+"):  # 表头分隔线
            continue
        if line.startswith("(") and "rows)" in line:  # (N rows)
            continue
        sections[current].append(line)
    return sections


def normalize_row(row: str, section: str) -> str | None:
    # 表格数据行按 | 切列
    if "|" not in row:
        return row
    cols = [c.strip() for c in row.split("|")]
    # 段10 约束类型分布：剔除 PG18 的 NOT NULL contype='n'
    if section == "10" and len(cols) >= 4 and cols[2] == "n":
        return None
    # 段5/7/8 ACL：规范化 owner 的 m 权限位（PG18 MAINTAIN）。
    # 只处理 grantor 为 postgres 的 owner 条目：形如 postgres=arwdDxtm/postgres
    cols = [
        re.sub(r"(postgres=[a-z]*Dxt)m(/postgres)", r"\1\2", c)
        for c in cols
    ]
    # 规范化列间空白
    return " | ".join(cols)


def compare(s16: dict[str, list[str]], s18: dict[str, list[str]]) -> tuple[int, list[str]]:
    problems: list[str] = []
    total_sections = 0
    all_keys = sorted(set(s16) | set(s18), key=lambda x: (x == "PREAMBLE", x))
    for sec in all_keys:
        if sec == "PREAMBLE":
            continue
        total_sections += 1
        a = {normalize_row(r, sec) for r in s16.get(sec, [])}
        b = {normalize_row(r, sec) for r in s18.get(sec, [])}
        a.discard(None)
        b.discard(None)
        only16 = a - b
        only18 = b - a
        if only16 or only18:
            problems.append(f"段 {sec}: 仅PG16={len(only16)} 行, 仅PG18={len(only18)} 行")
            for r in sorted(only16)[:8]:
                problems.append(f"  [仅16] {r}")
            for r in sorted(only18)[:8]:
                problems.append(f"  [仅18] {r}")
    return total_sections, problems


def main() -> int:
    s16 = parse(ROOT / "probe-pg16-16.15.txt")
    s18 = parse(ROOT / "probe-pg18-18.7.txt")
    n, problems = compare(s16, s18)
    print(f"对比段数: {n} (probe.sql 共 11 段)")
    if not problems:
        print("RESULT: PASS — 规范化后两库业务 catalog 逐段完全一致，无回退")
        return 0
    print(f"RESULT: FAIL — {len(problems)} 处差异")
    for p in problems:
        print(p)
    return 1


if __name__ == "__main__":
    sys.exit(main())
