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

B. 静态与契约检查编排（``uv run python scripts/merge_readiness.py --full``）：
   逐项调用项目已有的静态/契约检查器（ruff / mypy / contract / arch / freeze）。
   **这不是完整 CI**：pytest 全量、权限负向测试、PostgreSQL 迁移升级、
   downgrade→upgrade 往返、Docker 镜像构建均以 CI 为最终依据（见 .github/workflows/ci.yml）。
   命名上「full」指本脚本能编排的静态与契约项全集，不承诺覆盖 CI 的运行时与镜像检查——
   误把静态绿灯当成完整合并验收，正是要防的沉默失败。

设计原则（与 architecture_check.py 一致）：判据写明理由；fail-closed；
覆盖面防呆；有负向测试（tests/engineering/test_merge_readiness.py）；
不把「本次教训列表」硬编码成白名单/黑名单（那会重蹈清单腐烂的覆辙）。

C. 远端基线新鲜度（``--git`` 模式，G5 的机器判定）：
   - 真实 fetch，失败标 BLOCKED-ENV（不是 PASS、不是代码 FAIL）：
     没有网络/远端对象就没有证据，禁止基于旧基线合并；
   - 三方 SHA 核对（GitHub API 远端 main / 本地 origin/main / 目标 HEAD）；
   - 目标提交对象可读、提交结构合法（tree 行 + 合法父 sha；根提交可无父）。
   退出码三态：0=PASS / 1=代码缺陷 FAIL / 2=BLOCKED-ENV 环境阻塞。

用法：
    轻量（只静态派生）：  uv run python scripts/merge_readiness.py
    完整（含 CI 全口径）：uv run python scripts/merge_readiness.py --full
    含 git 前置 + 远端基线：uv run python scripts/merge_readiness.py --git
"""

from __future__ import annotations

import argparse
import ast
import shutil
import subprocess
import sys
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 覆盖面清单**不在这里维护第二份**——从 architecture_check 导入，单一事实源。
# 自己抄一份 LAYERS 正是本脚本要防的病（清单不会自己举手，漏一个就免疫）。
# mypy 的检查目录同样派生（scripts/mypy_targets.py），与 CI 共用同一个口径：
# 之前 CI 写死四个目录、这里再写一遍，adapters 落地真实实现后两边都没覆盖到它，
# 而「漏掉」这件事不会报错。scripts/ 自身不在 mypy 范围（既有口径）。
from architecture_check import LAYERS, NON_LAYER_DIRS  # noqa: E402
from mypy_targets import mypy_targets  # noqa: E402

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
    ("mypy 类型检查", ("mypy", *mypy_targets())),
    ("契约派生一致性", (sys.executable, "scripts/contract_check.py")),
    ("分层依赖校验", (sys.executable, "scripts/architecture_check.py")),
    ("契约冻结校验", (sys.executable, "scripts/freeze_manifest.py", "--check")),
)


@dataclass(frozen=True)
class Finding:
    """一条可机检的发现。rule 是规则号，location 是 file:line，message 说明原因。

    ``blocked`` 标记「环境阻塞」而非代码缺陷（G5 三态里的 BLOCKED-ENV）：
    API/网络/远端对象不可得时，既不能当作 PASS，也不应记成代码 FAIL。
    它同样阻止合并（返回非零），但输出与退出码与真实缺陷区分，便于人工裁定。
    """

    rule: str
    location: str
    message: str
    blocked: bool = False


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
    if rc != 0 or branch in ("HEAD", ""):
        findings.append(Finding("G2 分支新鲜度", "git rev-parse", "无法解析当前分支名，禁止在 detached HEAD 上合并"))
        return findings

    # 远端引用必须存在：缺失时 fail-closed，不静默当作「不落后」。
    rc_ref, remote_sha = git_out("rev-parse", "--verify", "refs/remotes/origin/main")
    if rc_ref != 0 or not remote_sha:
        findings.append(
            Finding(
                "G2 分支新鲜度",
                "origin/main",
                "远端跟踪引用不存在；先 git fetch origin main，无法确认基线新鲜度则禁止合并",
            )
        )
        return findings

    # revision range 方向：`A..B` 统计「B 可达但 A 不可达」的提交。
    # 落后数 = {branch}..origin/main（主干有、分支没有）；写反会数成领先数，
    # 导致 feature 分支的正常开发提交被误报为落后，而真正落后反而漏报。
    rc2, behind_raw = git_out("rev-list", "--count", f"{branch}..origin/main")
    if rc2 != 0 or not behind_raw.isdigit():
        findings.append(
            Finding(
                "G2 分支新鲜度",
                branch,
                f"无法计算落后提交数（git rev-list 退出码 {rc2}，输出 {behind_raw!r}）；"
                "证据不足不得当作通过",
            )
        )
    else:
        behind = int(behind_raw)
        if behind > 0:
            findings.append(
                Finding(
                    "G2 分支新鲜度",
                    branch,
                    f"当前分支落后 origin/main {behind} 个提交；"
                    "先 git fetch && git rebase/pull，避免把过期基线当合并起点",
                )
            )
    return findings


# --------------------------------------------------------------------------- #
# G5：远端基线新鲜度（把 runbook 的 BLOCKED-ENV 变成机器判定）
# --------------------------------------------------------------------------- #
# 外部 I/O 收敛为可注入 callable：默认走真实 git / gh，测试注入假实现，
# 判据逻辑保持纯函数式——「证据不足」与「证据表明不一致」必须能被分别证明。
def _real_fetch(root: Path) -> tuple[int, str]:
    p = subprocess.run(
        ("git", "fetch", "origin", "main"), cwd=root,
        capture_output=True, text=True, check=False,
    )
    return p.returncode, (p.stderr + p.stdout).strip()


def _real_api_sha(root: Path) -> tuple[int, str]:
    """从 GitHub API 读远端 main 的真实 SHA（权威事实源，非本地跟踪引用）。"""
    p = subprocess.run(
        ("gh", "api", "repos/{owner}/{repo}/git/ref/heads/main", "--jq", ".object.sha"),
        cwd=root, capture_output=True, text=True, check=False,
    )
    return p.returncode, p.stdout.strip()


def _real_cat_file(root: Path, sha: str) -> tuple[int, str]:
    p = subprocess.run(
        ("git", "cat-file", "-p", sha), cwd=root,
        capture_output=True, text=True, check=False,
    )
    return p.returncode, p.stdout


def _is_hex_sha(value: str) -> bool:
    """判断是否为合法 git 对象 sha（SHA-1 40 位或 SHA-256 64 位十六进制）。"""
    return len(value) in (40, 64) and all(c in "0123456789abcdefABCDEF" for c in value)


def check_remote_baseline(
    root: Path = ROOT,
    *,
    fetch_fn: Callable[[Path], tuple[int, str]] | None = None,
    api_sha_fn: Callable[[Path], tuple[int, str]] | None = None,
    cat_file_fn: Callable[[Path, str], tuple[int, str]] | None = None,
) -> list[Finding]:
    """G5：合并前证明远端基线新鲜 + 三方一致，环境不可得时标 BLOCKED-ENV。

    判据（对应 runbook G5 与用户工程裁决）：
    1. 必须执行真实 fetch；fetch 失败 = 环境阻塞（BLOCKED-ENV），**不是 PASS、
       也不是代码缺陷**。没有通道就没有证据，不得据此做需要最新基线的合并。
    2. fetch 成功后核对三方 SHA：GitHub API 的远端 main、本地 origin/main、
       目标提交（当前分支 HEAD 或指定提交）。任一不等 = 证据表明不一致 = FAIL。
    3. 目标提交对象必须可读、提交结构合法（有 tree、父 sha 合法；根提交可无父）；
       否则证据不足 = FAIL。
    4. 三项全部满足才无 finding（PASS）。

    注：本检查带网络依赖，仅在本地 ``--git`` 模式运行；CI 无远端历史，不跑此项。
    """
    findings: list[Finding] = []
    fetch = fetch_fn or _real_fetch
    api_sha = api_sha_fn or _real_api_sha
    cat_file = cat_file_fn or _real_cat_file

    # 1) 真实 fetch —— 环境阻塞与代码缺陷在此分流。
    rc_fetch, fetch_out = fetch(root)
    if rc_fetch != 0:
        findings.append(
            Finding(
                "G5 远端基线",
                "git fetch origin main",
                f"BLOCKED-ENV：无法执行真实 fetch（退出码 {rc_fetch}）——"
                f"{fetch_out[:200]}。没有通道就没有证据，禁止基于旧基线合并/变基/发布；"
                "这不记作代码缺陷，但绝不输出 PASS。",
                blocked=True,
            )
        )
        return findings

    def git_out(*args: str) -> tuple[int, str]:
        p = subprocess.run(
            ("git", *args), cwd=root, capture_output=True, text=True, check=False
        )
        return p.returncode, p.stdout.strip()

    rc_api, api_main = api_sha(root)
    if rc_api != 0 or not api_main:
        findings.append(
            Finding(
                "G5 远端基线",
                "gh api git/ref/heads/main",
                f"BLOCKED-ENV：无法读取远端 main 权威 SHA（退出码 {rc_api}）——"
                "没有远端事实源就无法核对本地跟踪引用是否新鲜，"
                "禁止基于旧基线合并；不记作代码缺陷，但绝不输出 PASS。",
                blocked=True,
            )
        )
        return findings

    rc_ref, track_sha = git_out("rev-parse", "--verify", "refs/remotes/origin/main")
    rc_head, target_sha = git_out("rev-parse", "--verify", "HEAD")
    if rc_ref != 0 or rc_head != 0 or not track_sha or not target_sha:
        findings.append(
            Finding(
                "G5 远端基线",
                "git rev-parse",
                "fetch 后无法解析 origin/main 或 HEAD；证据不足，禁止合并。",
            )
        )
        return findings

    # 2) 三方 SHA 一致性（API 远端 / 本地跟踪引用 / 目标提交）。
    if not (api_main == track_sha == target_sha):
        findings.append(
            Finding(
                "G5 远端基线",
                "三方 SHA",
                f"远端 main / origin/main / HEAD 不一致："
                f"api={api_main[:12]} track={track_sha[:12]} head={target_sha[:12]}；"
                "先 git fetch && git merge/rebase 到最新远端基线再合并。",
            )
        )

    # 3) 目标提交对象可读 + 提交结构可解析（对象级一致性，非 tree 级）。
    #    判据是「确实是合法 commit 对象」：必须有 tree 行；若有 parent 行则其 sha
    #    必须是合法 hex。根提交没有 parent 是合法 Git 语义，不因缺 parent 误伤；
    #    但对象损坏（无 tree / 非法 parent sha）不能被放过。
    rc_obj, raw = cat_file(root, target_sha)
    if rc_obj != 0 or not raw:
        findings.append(
            Finding(
                "G5 远端基线",
                target_sha[:12],
                "目标提交对象不可读；合并对象的字节无法核验，禁止合并。",
            )
        )
    else:
        lines = raw.splitlines()
        has_tree = any(ln.startswith("tree ") and len(ln.split()) == 2 for ln in lines)
        parents = [
            ln.split()[1]
            for ln in lines
            if ln.startswith("parent ") and len(ln.split()) == 2
        ]
        bad_parent = [p for p in parents if not _is_hex_sha(p)]
        if not has_tree or bad_parent:
            findings.append(
                Finding(
                    "G5 远端基线",
                    target_sha[:12],
                    f"目标提交对象结构非法（缺 tree 行={not has_tree}，"
                    f"非法父 sha={bad_parent[:2]}）；提交对象无法可信核验，禁止合并。",
                )
            )
    return findings


def resolve_command(root: Path, cmd: tuple[str, ...]) -> list[str]:
    """把 ``CHECK_COMMANDS`` 的一项解析成可执行命令列表。

    ruff/mypy 优先用项目虚拟环境的可执行文件，保证与 CI（uv.lock）同版本；
    其余项本身就是 ``sys.executable <script>``，不需要解析。

    ``scripts/verification_battery.py`` 复用它而不是自己抄一份：
    两份解释器解析逻辑迟早会分叉，而分叉出来的那处只会表现为「本地绿、CI 红」。
    """
    resolved = list(cmd)
    if resolved[0] in ("ruff", "mypy"):
        exe = root / ".venv" / "Scripts" / f"{resolved[0]}.exe"
        if not exe.exists():
            exe = root / ".venv" / "bin" / resolved[0]
        if exe.exists():
            resolved[0] = str(exe)
    return resolved


def run_external_checks(root: Path = ROOT) -> list[Finding]:
    """B 类：逐项跑 CI 口径检查器。任何一项非零退出即记为发现，fail-closed。"""
    findings: list[Finding] = []
    for name, cmd in CHECK_COMMANDS:
        resolved = resolve_command(root, cmd)
        p = subprocess.run(resolved, cwd=root, capture_output=True, text=True, check=False)
        if p.returncode != 0:
            tail = "\n".join((p.stdout + p.stderr).strip().splitlines()[-12:])
            findings.append(Finding("C CI 口径", name, f"检查器退出码 {p.returncode}:\n{tail}"))
    return findings


def run_all(root: Path = ROOT, *, full: bool = False, git: bool = False) -> list[Finding]:
    findings = check_static(root)
    if git:
        findings += check_git(root)
        findings += check_remote_baseline(root)
    if full:
        findings += run_external_checks(root)
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description="合并前自检（派生式判据 + CI 口径编排）")
    parser.add_argument(
        "--full",
        action="store_true",
        help="额外跑 ruff/mypy/contract/architecture/freeze 静态与契约检查（不含 pytest/DB/镜像，完整验收以 CI 为准）",
    )
    parser.add_argument(
        "--git",
        action="store_true",
        help="额外检查工作树洁净度、分支是否落后 origin/main、以及 G5 远端基线新鲜度"
        "（真实 fetch + 三方 SHA + 对象核验；网络不可得标 BLOCKED-ENV，退出码 2）",
    )
    args = parser.parse_args()

    findings = run_all(ROOT, full=args.full, git=args.git)
    code_faults = [f for f in findings if not f.blocked]
    blocked = [f for f in findings if f.blocked]

    for f in code_faults:
        print(f"[FAIL] {f.rule} | {f.location} | {f.message}")
    for f in blocked:
        print(f"[BLOCKED-ENV] {f.rule} | {f.location} | {f.message}")

    # 三态：代码缺陷优先（exit 1）；仅环境阻塞 = BLOCKED-ENV（exit 2，绝不报 PASS）；
    # 全绿才 PASS（exit 0）。环境阻塞不记作代码 FAIL，但同样阻止合并决策。
    if code_faults:
        tail = f"（另有 {len(blocked)} 项环境阻塞）" if blocked else ""
        print(f"[FAIL] 合并前自检未通过：{len(code_faults)} 项代码/判据缺陷{tail}")
        return 1
    if blocked:
        print(
            f"[BLOCKED-ENV] 合并前自检受阻：{len(blocked)} 项环境阻塞——"
            "证据不足，不宣称一致；禁止据此做需要最新基线的合并/变基/发布（G5）"
        )
        return 2

    mode = "静态派生" + (" + git" if args.git else "") + (" + 静态与契约检查" if args.full else "")
    print(f"[PASS] 合并前自检通过（{mode}）：字典重复键 0 / 同名顶层定义 0 / 覆盖面防呆 OK"
          + (" / git 前置 OK" if args.git else "")
          + (" / 静态与契约检查全绿（pytest/DB/镜像仍以 CI 为准）" if args.full else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
