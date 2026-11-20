"""从 git 对象（而不是工作区）建立两支的 Alembic 迁移图。

为什么要单独写这个工具
----------------------
对账时最容易犯的错是「看工作区的文件名」。工作区只有一个分支的检出，
而同一 revision ID 在另一支可能是另一份实现（本项目已实测：``b3c4d5e6f7a8``）。
因此必须从 **固定提交** 的 git 对象读取，并同时比较三件事：

1. revision ID 集合（谁是「两支都有」）；
2. 同一 ID 的**文件名是否相同**（不同文件名 = 语义不同的强证据）；
3. 同一 ID 同一文件名的**内容哈希是否相同**（内容不同则仍需逐句对账）。

输出是 markdown，直接贴进对账报告；不写数据库、不改分支。
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REVISION_RE = re.compile(r"^revision(?:\s*:\s*str)?\s*=\s*[\"']([^\"']+)[\"']", re.MULTILINE)
DOWN_RE = re.compile(r"^down_revision(?:\s*:[^=]+)?\s*=\s*(.+)$", re.MULTILINE)
VERSIONS_DIR = "data/migrations/versions"


@dataclass(frozen=True)
class Migration:
    branch: str
    path: str
    revision: str
    down_revision: str | None
    sha256: str

    @property
    def filename(self) -> str:
        return Path(self.path).name


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout


def _parse_down(raw: str) -> str | None:
    value = raw.strip()
    if value in {"None", "()", "[]"}:
        return None
    if value.startswith("("):
        ids = re.findall(r"[\"']([^\"']+)[\"']", value)
        if len(ids) != 1:
            raise ValueError(f"暂不支持多父 down_revision：{value}")
        return ids[0]
    match = re.match(r"[\"']([^\"']+)[\"']", value)
    if not match:
        raise ValueError(f"无法解析 down_revision：{value}")
    return match.group(1)


def load_branch(repo: Path, ref: str) -> list[Migration]:
    files = [
        line
        for line in _git(repo, "ls-tree", "-r", "--name-only", ref, "--", VERSIONS_DIR).splitlines()
        if line.endswith(".py") and "__init__" not in line
    ]
    migrations: list[Migration] = []
    for path in sorted(files):
        source = _git(repo, "show", f"{ref}:{path}")
        revision = REVISION_RE.search(source)
        down = DOWN_RE.search(source)
        if revision is None or down is None:
            raise ValueError(f"{ref}:{path} 缺少 revision/down_revision")
        migrations.append(
            Migration(
                branch=ref,
                path=path,
                revision=revision.group(1),
                down_revision=_parse_down(down.group(1)),
                sha256=hashlib.sha256(source.encode("utf-8")).hexdigest(),
            )
        )
    return migrations


def heads(migrations: list[Migration]) -> list[str]:
    children = {m.down_revision for m in migrations if m.down_revision}
    return sorted(m.revision for m in migrations if m.revision not in children)


def chain(migrations: list[Migration], head: str) -> list[str]:
    by_id = {m.revision: m for m in migrations}
    order: list[str] = []
    current: str | None = head
    while current:
        order.append(current)
        current = by_id[current].down_revision
    return list(reversed(order))


def main() -> int:
    repo = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    refs = sys.argv[2:] or ["main", "origin/feat/identity-data-ownership"]
    left_ref, right_ref = refs[0], refs[1]
    left = load_branch(repo, left_ref)
    right = load_branch(repo, right_ref)

    for ref, items in ((left_ref, left), (right_ref, right)):
        print(f"\n## {ref}（{len(items)} 个迁移文件）\n")
        head_ids = heads(items)
        print(f"head: {', '.join(head_ids)}")
        for head in head_ids:
            print(f"链长 {len(chain(items, head))}：{' → '.join(chain(items, head))}")

    left_ids = {m.revision for m in left}
    right_ids = {m.revision for m in right}
    left_by_id = {m.revision: m for m in left}
    right_by_id = {m.revision: m for m in right}

    print("\n## 两支 ID 差异\n")
    print(f"仅 {left_ref}：{sorted(left_ids - right_ids)}")
    print(f"仅 {right_ref}：{sorted(right_ids - left_ids)}")

    print("\n## 同 ID 对照（文件名 / 内容哈希）\n")
    print("| revision | 左文件名 | 右文件名 | 文件名相同 | 内容相同 | 判定 |")
    print("| --- | --- | --- | --- | --- | --- |")
    for revision in sorted(left_ids & right_ids):
        left_m = left_by_id[revision]
        right_m = right_by_id[revision]
        same_name = left_m.filename == right_m.filename
        same_content = left_m.sha256 == right_m.sha256
        if same_name and same_content:
            verdict = "一致"
        elif not same_name:
            verdict = "**语义不同（硬冲突）**"
        else:
            verdict = "同名但内容不同（需逐句对账）"
        print(
            f"| `{revision}` | {left_m.filename} | {right_m.filename} | "
            f"{'是' if same_name else '否'} | {'是' if same_content else '否'} | {verdict} |"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
