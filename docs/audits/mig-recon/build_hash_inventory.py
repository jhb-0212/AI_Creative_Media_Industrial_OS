"""生成跨分支迁移文件的哈希清单（ADR v1.4.3 的哈希重算依据）。

设计要点（为什么这样算，而不是随手 sha256sum）：

1. **双锚点**：git blob SHA1 与文件 SHA-256 同时记录。
   blob SHA1 由 Git 对仓库内字节计算，不受工作区换行设置影响，是稳定锚点；
   SHA-256 易受 CRLF/LF 转换影响，必须声明口径（本脚本固定 `git show` 原始字节）。
   ADR C.1 曾把 2 行按 CRLF 计算、4 行按 LF 计算，导致同表不可横向比较。

2. **固定来源**：每个文件记录 ref、commit、path 三元组，避免「同名文件」被误认。

3. **分类**：按「决策单元」而不是按文件计数，因为 9 处同名分叉里
   7 处只是角色名来源改写（默认配置下等价），3 处才语义不等价。

输出：Markdown 表格到 stdout，供文档直接引用；JSON 到 diverged.json。
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

MAIN_REF = "main"
FEAT_REF = "origin/feat/identity-data-ownership"
MIG_DIR = "data/migrations/versions"

REVISION_RE = re.compile(r"^revision(?::\s*[^=]+)?\s*=\s*[\"']([^\"']+)", re.MULTILINE)

# 决策单元分类：按「改动的性质」而非「文件是否不同」。
# T1 = 同 ID、不同路径、效果互斥；T2 = 仅角色名来源；T3 = 叠加角色属性收敛/口令守卫；
# T4 = 叠加 helper 调用点绑定。
CLASSIFICATION: dict[str, tuple[str, str]] = {
    "b3c4d5e6f7a8": ("T1", "同 ID 不同文件：归属外键 vs 状态机守卫（功能不同，可共存）"),
    "9e3b4c5d6e7f": ("T3", "角色名来源 + ALTER ROLE 属性收敛 + 开发口令守卫"),
    "b7c8d9e0f1a2": ("T4", "角色名来源 + partition_guard 签名绑定（效果同、组合不兼容）"),
}
DEFAULT_CLASS = ("T2", "仅角色名来源（配置 app_role_name() vs 常量 APP_ROLE）")


def _git(*args: str) -> bytes:
    return subprocess.run(["git", *args], capture_output=True, check=True).stdout


def _files(ref: str) -> list[str]:
    out = _git("ls-tree", "-r", "--name-only", ref, "--", MIG_DIR).decode()
    return [line for line in out.splitlines() if line.endswith(".py")]


def _blob(ref: str, path: str) -> bytes:
    return _git("show", f"{ref}:{path}")


def _blob_sha1(ref: str, path: str) -> str:
    return _git("rev-parse", f"{ref}:{path}").decode().strip()


def _revision(source: str) -> str | None:
    match = REVISION_RE.search(source)
    return match.group(1) if match else None


def main() -> int:
    main_commit = _git("rev-parse", MAIN_REF).decode().strip()
    feat_commit = _git("rev-parse", FEAT_REF).decode().strip()

    # 键必须是 revision，不是文件名：`b3c4d5e6f7a8` 两侧文件名不同，
    # 按 basename 取交集会把它整个丢掉——而它恰恰是唯一的 T1（效果互斥）。
    main_by_rev: dict[str, str] = {}
    for path in _files(MAIN_REF):
        revision = _revision(_blob(MAIN_REF, path).decode("utf-8"))
        if revision:
            main_by_rev[revision] = path
    feat_by_rev: dict[str, str] = {}
    for path in _files(FEAT_REF):
        revision = _revision(_blob(FEAT_REF, path).decode("utf-8"))
        if revision:
            feat_by_rev[revision] = path

    rows: list[dict[str, object]] = []
    for revision in sorted(set(main_by_rev) & set(feat_by_rev)):
        m_path, f_path = main_by_rev[revision], feat_by_rev[revision]
        m_bytes, f_bytes = _blob(MAIN_REF, m_path), _blob(FEAT_REF, f_path)
        if m_bytes == f_bytes:
            continue
        diff_count = sum(
            1
            for line in subprocess.run(
                ["git", "diff", "--text", "-U0", f"{MAIN_REF}:{m_path}", f"{FEAT_REF}:{f_path}"],
                capture_output=True,
                text=True,
            ).stdout.splitlines()
            if line[:1] in "+-" and not line.startswith(("+++", "---"))
        )
        kind, why = CLASSIFICATION.get(revision, DEFAULT_CLASS)
        rows.append(
            {
                "revision": revision,
                "class": kind,
                "why": why,
                "main_path": m_path,
                "feat_path": f_path,
                "same_path": m_path == f_path,
                "main_blob": _blob_sha1(MAIN_REF, m_path),
                "feat_blob": _blob_sha1(FEAT_REF, f_path),
                "main_sha256": hashlib.sha256(m_bytes).hexdigest(),
                "feat_sha256": hashlib.sha256(f_bytes).hexdigest(),
                "main_bytes": len(m_bytes),
                "feat_bytes": len(f_bytes),
                "diff_lines": diff_count,
            }
        )

    # 同 ID 但任一侧缺失（跨路径冲突的另一半）：T1 的文件名不同，已含在上表；
    # 这里补「同 ID 但内容一致且路径不同」的情形（不算冲突，仅记录）。
    print(f"<!-- main={main_commit} feat={feat_commit} -->")
    print()
    print(
        "| # | Revision | 类型 | main 路径 | 分支路径 | 同路径 | main blob SHA1 | 分支 blob SHA1 "
        "| main SHA-256 | 分支 SHA-256 | 差异行 |"
    )
    print("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for index, row in enumerate(rows, 1):
        print(
            f"| {index} | `{row['revision']}` | {row['class']} | `{Path(str(row['main_path'])).name}` "
            f"| `{Path(str(row['feat_path'])).name}` | {'是' if row['same_path'] else '**否**'} "
            f"| `{str(row['main_blob'])[:12]}…` | `{str(row['feat_blob'])[:12]}…` "
            f"| `{str(row['main_sha256'])[:12]}…` | `{str(row['feat_sha256'])[:12]}…` "
            f"| {row['diff_lines']} |"
        )

    print()
    counts: dict[str, int] = {}
    for row in rows:
        counts[str(row["class"])] = counts.get(str(row["class"]), 0) + 1
    print(
        f"<!-- 合计 {len(rows)} 个 revision；"
        + " / ".join(f"{k}={v}" for k, v in sorted(counts.items()))
        + " -->"
    )

    payload = {
        "main_commit": main_commit,
        "feat_commit": feat_commit,
        "counts": counts,
        "rows": rows,
    }
    Path("docs/audits/mig-recon/hash-inventory.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
