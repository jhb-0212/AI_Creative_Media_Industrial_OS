"""验证 T2（仅角色名来源）是否「只差角色名」，并分离出真实语义差异。

方法：把两侧源码里的角色名表达式统一替换成占位符，剔除与角色名来源有关的
**形式差异**（导入行、常量定义行），再用 difflib 做序列比对。

- 若归一化后无残差 → 该 revision 在 `POSTGRES_APP_USER=app_role`（默认配置）下代码路径等价；
- 若仍有残差 → 残差行就是 T3/T4 的真实差异，必须逐条阅读，不得笼统归入 T2。

归一化项（每项都对应一个已知形式差异，不含任何语义裁剪）：
1. `app_role_name()`（main，配置驱动）→ `ROLE_NAME`
2. `APP_ROLE`（feat，模块常量）→ `ROLE_NAME`
3. `ROLE_NAME = "app_role"` 常量定义行 → 删除（main 无对应定义）
4. `from packages.shared.db_roles import ...` / `import db_roles` → 删除
5. 空行 → 删除（纯排版）

**边界**：本脚本只证明「迁移源码等价」，不证明「数据库效果等价」。
后者须由步进快照实测（2026-11-20-dual-path-replay-experiment.md R2）。
"""

from __future__ import annotations

import difflib
import re
import subprocess
import sys

MAIN_REF = "main"
FEAT_REF = "origin/feat/identity-data-ownership"
MIG_DIR = "data/migrations/versions"

ROLE_CALL_RE = re.compile(r"app_role_name\(\s*\)")
# 两侧各自把角色名放进常量，命名不统一：`APP_ROLE`（模块级）与 `_APP_ROLE`（局部）。
# 用「非标识符前缀」而不是 `\b`——`\b` 在 `_APP_ROLE` 里不成立（下划线是词字符），
# 会把一半的调用点漏掉，正是初版误报 5 行残差的原因。
ROLE_CONST_RE = re.compile(r"(?<![A-Za-z0-9])_*APP_ROLE\b")
ROLE_DEF_RE = re.compile(r"^ROLE_NAME\s*=\s*[\"']app_role[\"']$")
DB_ROLES_IMPORT_RE = re.compile(r"^\s*from\s+packages\.shared(\.db_roles)?\s+import\b.*$")
REVISION_RE = re.compile(r"^revision(?::\s*[^=]+)?\s*=\s*[\"']([^\"']+)", re.MULTILINE)


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, check=True, text=True).stdout


def normalize(source: str) -> list[str]:
    text = ROLE_CALL_RE.sub("ROLE_NAME", source)
    text = ROLE_CONST_RE.sub("ROLE_NAME", text)
    lines = [line.rstrip() for line in text.splitlines()]
    return [
        line
        for line in lines
        if line.strip()
        and not ROLE_DEF_RE.match(line.strip())
        and not DB_ROLES_IMPORT_RE.match(line)
    ]


def revision_map(ref: str) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for path in _git("ls-tree", "-r", "--name-only", ref, "--", MIG_DIR).splitlines():
        if not path.endswith(".py"):
            continue
        match = REVISION_RE.search(_git("show", f"{ref}:{path}"))
        if match:
            mapping[match.group(1)] = path
    return mapping


def main() -> int:
    revisions = [arg.strip() for arg in sys.argv[1:] if arg.strip()]
    if not revisions:
        print("用法: python verify_t2_equivalence.py <revision>...")
        return 2

    main_map, feat_map = revision_map(MAIN_REF), revision_map(FEAT_REF)
    for revision in revisions:
        if revision not in main_map or revision not in feat_map:
            print(f"{revision}: 仅一侧存在，跳过等价性检验（属集合差异，不是内容分叉）")
            continue
        left = normalize(_git("show", f"{MAIN_REF}:{main_map[revision]}"))
        right = normalize(_git("show", f"{FEAT_REF}:{feat_map[revision]}"))
        if left == right:
            print(f"{revision}: 归一化后逐行一致（{len(left)} 行）→ 仅差角色名来源")
            continue
        matcher = difflib.SequenceMatcher(None, left, right, autojunk=False)
        residue = sum(
            (i2 - i1) + (j2 - j1) for tag, i1, i2, j1, j2 in matcher.get_opcodes() if tag != "equal"
        )
        print(
            f"{revision}: 归一化后残差 {residue} 行（相似度 {matcher.ratio():.3f}，main {len(left)} 行 / feat {len(right)} 行）"
        )
        shown = 0
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal" or shown >= 6:
                continue
            for line in left[i1:i2][:3]:
                print(f"    - {line.strip()[:110]}")
                shown += 1
            for line in right[j1:j2][:3]:
                print(f"    + {line.strip()[:110]}")
                shown += 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
