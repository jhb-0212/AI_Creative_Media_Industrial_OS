"""合并前自检闸门（多分支整合暴露的教训的机械化沉淀）。

为什么需要它
------------
2026-11-21 的双线整合（A13 Scene/Shot 运行时 + 本地归属治理线）一次性暴露了
三类**沉默缺陷**——它们在各自分支上都不报错，只在合并点集中爆发：

1. 字典字面量重复键：`_RESOLVE_SQL` 的 Scene/Shot 各定义两次，Python 后键
   静默覆盖前键，旧 Shot 指向已删 schema。不报错、不进任何现有闸门。
2. 同名顶层定义：同模块里后定义的 class/def 覆盖前者，同样是静默覆盖。
3. CI 盲区：本地治理线从不推主干、从不过 CI，累积 122 项 lint 债，
   直到合并才发现；契约编号也各自占用，撞成 retroactive 重编号。

本脚本把这些做成**可执行、可复算、能 FAIL** 的判据，而不是一份会腐烂的
人工清单（清单不会自己举手——这正是病根本身）。两类能力：

A. 静态派生检查（默认跑，无需外部依赖）：
   - D1 字典字面量常量重复键（AST）
   - D2 模块级同名顶层定义（AST）
   覆盖范围 R0 防呆：代码目录清单必须与 architecture_check 的 LAYERS ∪
   NON_LAYER_DIRS 对齐，新增目录会被点名，避免新目录天然免疫检查。

B. CI 口径编排（``uv run python scripts/merge_readiness.py --full``）：
   逐项调用项目已有的事实源检查器，任何一项红即整体红。这是治「CI 盲区」
   的根：把散落的闸门收成一次合并前必跑的动作。被编排的检查器是唯一事实源，
   本脚本不复制它们的判据（避免第二份真相漂移）。

设计原则（与 architecture_check.py 一致）：判据写明理由；fail-closed；
覆盖面防呆；有负向测试（tests/engineering/test_merge_readiness.py）；
不把「本次教训列表」硬编码成白名单/黑名单（那会重蹈清单腐烂的覆辙）。

用法：
    轻量（只静态派生）：  uv run python scripts/merge_readiness.py
    完整（含 CI 全口径）：uv run python scripts/merge_readiness.py --full
    仅检查工作树/分支关系：uv run python scripts/merge_readiness.py --git
"""

from __future__ import annotations

import argparse
import ast
import shutil
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 覆盖面清单**不在这里维护第二份**——从 architecture_check 导入，单一事实源。
# 自己抄一份 LAYERS 正是本脚本要防的病（清单不会自己举手，漏一个就免疫）。
# 注：CI mypy 口径只覆盖 packages/services/apps/workers，不含 scripts；
# 本模块自身类型已独立校验通过，architecture_check 的既有类型瑕疵不在本闸门范围。
from architecture_check import LAYERS, NON_LAYER_DIRS  # noqa: E402

# 仅用于跳过的缓存/产物目录（含 .py 的副本也不算源码落地处）。
IGNORE_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".venv",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "dist",
        "artifacts",
        "backups",
    }
)

# --full 编排的检查器。每项 (名称, 命令)。命令用 sys.executable 跑当前环境的
# python，避免误用系统解释器（本仓依赖 yaml 等，系统 python 没有 → 假红）。
# ruff/mypy 直接调可执行文件，与 CI 的 `uv run` 同版本（uv.lock 锁定）。
CHECK_COMMANDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("ruff 全仓 lint", ("ruff", "check", ".")),
    ("mypy 类型检查", ("mypy", "packages", "services", "apps", "workers")),
    ("契约派生一致性", (sys.executable, "scripts/contract_check.py")),
    ("分层依赖校验", (sys.executable, "scripts/architecture_check.py")),
    ("契约冻结校验", (sys.executable, "scripts/freeze_manifest.py", "--check")),
)


@dataclass(frozen=True)
class Finding:
    """一条可机检的发现。rule 是规则号，location 是 file:line，message 说明原因。"""

    rule: str
    location: str
    message: str


def iter_python_files(root: Path) -> list[Path]:
    """从文件系统派生待扫的 Python 文件全集，不走人工清单。

    扫描所有顶层目录（除 IGNORE_DIRS 缓存/产物），不依赖 LAYERS 清单——
    即使 architecture_check 漏登记某个新代码目录，AST 检查仍会覆盖它。
    """
    files: list[Path] = []
    for top in sorted(root.iterdir()):
        if not top.is_dir() or top.name.startswith(".") or top.name in IGNORE_DIRS:
            continue
        for path in top.rglob("*.py"):
            if any(part in IGNORE_DIRS for part in path.parts):
                continue
            files.append(path)
    return sorted(files)


def check_coverage(root: Path = ROOT) -> list[Finding]:
    """R0 覆盖面防呆（与 architecture_check.check_layer_coverage 同判据、同事实源）。

    判据理由：含 .py 的顶层目录若既不在 LAYERS 也不在 NON_LAYER_DIRS，
    就是一条新线引入的新层——architecture_check 的 R1-R3 会天然免疫它。
    清单从 architecture_check 导入，不在本脚本另存一份（单一事实源）。
    """
    findings: list[Finding] = []
    registered = set(LAYERS) | set(NON_LAYER_DIRS)
    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.name.startswith(".") or child.name in IGNORE_DIRS:
            continue
        if child.name in registered:
            continue
        if any(child.rglob("*.py")):
            findings.append(
                Finding(
                    "R0 覆盖面",
                    child.name,
                    f"顶层目录 {child.name!r} 含 .py 但未在 architecture_check 的 "
                    "LAYERS / NON_LAYER_DIRS 登记：请补登记，否则它天然免疫分层闸门",
                )
            )
    return findings


def _dict_duplicate_keys(tree: ast.AST) -> list[tuple[int, str]]:
    """返回字典字面量里的常量重复键 (行号, 键字面量)。

    判据理由：Python 对字典重复键是**静默覆盖**——不会报错、不会警告，
    只有最后一个生效。这正是 `_RESOLVE_SQL` 事故的机制：旧 Shot 定义被
    新 Shot 悄悄覆盖，代码照跑、测试照绿，死代码却掩盖了整合真相。
    """
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        seen: set[object] = set()
        for key in node.keys:
            if not isinstance(key, ast.Constant):
                continue  # 变量/表达式键无法静态判等，跳过（不制造假红）
            value = key.value
            if not isinstance(value, (str, int, float, bool, type(None))):
                continue
            if value in seen:
                out.append((key.lineno, repr(value)))
            seen.add(value)
    return out


def _duplicate_toplevel_defs(tree: ast.Module) -> list[tuple[int, str]]:
    """返回模块级同名 class/def/async def 的重复定义 (行号, 名称)。

    判据理由：与字典重复键同类——后定义静默覆盖前定义。合并两条线时若在
    同一模块各加一个同名工厂/常量函数，前者会无声消失。
    """
    seen: dict[str, list[int]] = defaultdict(list)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            seen[node.name].append(node.lineno)
    return [(lines[-1], name) for name, lines in seen.items() if len(lines) > 1]


def check_static(root: Path = ROOT) -> list[Finding]:
    """跑 A 类静态派生检查（D1 字典重复键 / D2 同名顶层定义 / R0 覆盖面）。"""
    findings = check_coverage(root)
    for path in iter_python_files(root):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (SyntaxError, UnicodeDecodeError) as exc:
            # 解析不了就是真问题，fail-closed（不静默跳过）
            findings.append(Finding("D0 语法解析", str(path), f"无法解析：{exc}"))
            continue
        rel = path.relative_to(root).as_posix()
        for lineno, key in _dict_duplicate_keys(tree):
            findings.append(
                Finding(
                    "D1 字典重复键",
                    f"{rel}:{lineno}",
                    f"字典字面量键 {key} 重复，Python 静默保留最后一个——请删掉被覆盖的定义",
                )
            )
        for lineno, name in _duplicate_toplevel_defs(tree):
            findings.append(
                Finding(
                    "D2 同名顶层定义",
                    f"{rel}:{lineno}",
                    f"模块级名称 {name!r} 重复定义，后者静默覆盖前者——请确认合并后只留一份",
                )
            )
    return findings


def check_git(root: Path = ROOT) -> list[Finding]:
    """检查合并前置的 git 事实：工作树洁净 + 当前分支未落后 origin/main。

    判据理由：在脏工作树或过期分支上做合并，会把「本地未提交的修复」或
    「主干已经合入的改动」当成新工作，产生难以追溯的分叉。两项都从 git
    实读，不靠人自觉。CI 环境通常没有完整历史/远端，故此项仅本地 --git 跑。
    """
    findings: list[Finding] = []
    if shutil.which("git") is None:
        findings.append(Finding("G0 环境", "git", "未找到 git 可执行文件，无法做 git 前置检查"))
        return findings

    def git_out(*args: str) -> tuple[int, str]:
        p = subprocess.run(
            ("git", *args), cwd=root, capture_output=True, text=True, check=False
        )
        return p.returncode, p.stdout.strip()

    rc, dirty = git_out("status", "--porcelain")
    if rc != 0:
        findings.append(Finding("G1 工作树", "git status", "无法读取工作树状态"))
    elif dirty:
        lines = dirty.splitlines()
        findings.append(
            Finding(
                "G1 工作树",
                "git status",
                f"工作树不干净（{len(lines)} 项未提交）；合并前先提交或暂存："
                + "; ".join(lines[:5]),
            )
        )

    rc, branch = git_out("rev-parse", "--abbrev-ref", "HEAD")
    if rc == 0 and branch not in ("HEAD", ""):
        rc2, behind = git_out("rev-list", "--count", f"origin/main..{branch}")
        if rc2 == 0 and behind.isdigit() and int(behind) > 0:
            findings.append(
                Finding(
                    "G2 分支新鲜度",
                    branch,
                    f"当前分支落后 origin/main {behind} 个提交；"
                    "先 git fetch && git rebase/pull，避免把过期基线当合并起点",
                )
            )
    return findings


def run_external_checks(root: Path = ROOT) -> list[Finding]:
    """B 类：逐项跑 CI 口径检查器。任何一项非零退出即记为发现，fail-closed。"""
    findings: list[Finding] = []
    for name, cmd in CHECK_COMMANDS:
        resolved = list(cmd)
        # ruff/mypy 优先用项目虚拟环境的 exe，保证与 CI（uv.lock）同版本
        if resolved[0] == "ruff":
            exe = root / ".venv" / "Scripts" / "ruff.exe"
            if exe.exists():
                resolved[0] = str(exe)
        elif resolved[0] == "mypy":
            exe = root / ".venv" / "Scripts" / "mypy.exe"
            if exe.exists():
                resolved[0] = str(exe)
        p = subprocess.run(resolved, cwd=root, capture_output=True, text=True, check=False)
        if p.returncode != 0:
            tail = "\n".join((p.stdout + p.stderr).strip().splitlines()[-12:])
            findings.append(Finding("C CI 口径", name, f"检查器退出码 {p.returncode}:\n{tail}"))
    return findings


def run_all(root: Path = ROOT, *, full: bool = False, git: bool = False) -> list[Finding]:
    findings = check_static(root)
    if git:
        findings += check_git(root)
    if full:
        findings += run_external_checks(root)
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description="合并前自检（派生式判据 + CI 口径编排）")
    parser.add_argument(
        "--full", action="store_true", help="额外跑 ruff/mypy/contract/architecture/freeze 全口径"
    )
    parser.add_argument(
        "--git", action="store_true", help="额外检查工作树洁净度与分支是否落后 origin/main"
    )
    args = parser.parse_args()

    findings = run_all(ROOT, full=args.full, git=args.git)
    for f in findings:
        print(f"[FAIL] {f.rule} | {f.location} | {f.message}")
    if findings:
        print(f"[FAIL] 合并前自检未通过：{len(findings)} 项发现")
        return 1

    mode = "静态派生" + (" + git" if args.git else "") + (" + CI 全口径" if args.full else "")
    print(f"[PASS] 合并前自检通过（{mode}）：字典重复键 0 / 同名顶层定义 0 / 覆盖面防呆 OK"
          + (" / git 前置 OK" if args.git else "")
          + (" / CI 口径全绿" if args.full else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
