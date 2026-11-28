"""CI 迁移往返深度的派生口径：不再写死 ``alembic downgrade -N`` 里的 N。

为什么不能写死
--------------
``alembic downgrade -N`` 是「新增迁移的 ``downgrade()`` 真能被数据库执行」的唯一
运行时证据（clean install 永远只走 upgrade）。N 写死时，**加了迁移却忘改 N 不会
报错**——它只是少撤一格：上一个迁移的降级从此没有任何运行时证据，而 CI 依旧全绿。

本仓库已经踩过两次同一个坑：

- ``-1`` 时代，``c5d6e7f8a9b0`` 的分区撤销写在 ``downgrade()`` 里却没人跑到，
  直到升级 CI 深度才暴露出 ``DROP ROLE`` 撞依赖；
- 截至 2026-11-25，本分支新增了 3 个迁移（``a7b8c9d0e1f2`` / ``c5d6e7f8a9b0`` /
  ``e7f8a9b0c1d2``），CI 仍写 ``-2``——最早那一个的降级从未在 CI 里执行过。

两次都不是「某条断言判错」，而是「N 是手抄的」。手抄的常量在这类判据里等于静默退化。

判据
----
N = ``git diff --name-status <merge-base>..HEAD -- data/migrations/versions``
里新增（A）与改写（M）的 ``.py`` 文件数。

- 用 **merge-base** 而不是 ``origin/main..HEAD``：分支落后 main 时，后者会把 main
  上别人的迁移也算进「本分支新增」，深度虚高且说不清来由。
- 改写（M）也计入：改一条已有迁移，同样需要它的 downgrade 有运行时证据。
- 出现删除（D）直接失败：迁移链不允许删条目——删一条会让「已升级的库」与「新建的
  库」停在同一个 revision 却不同结构，而这正是最难查的一类事故。
- 下限 1：增量为 0（例如在 main 上跑）时仍撤一格，保住「回滚后还能重建 head」这条
  证据；``-0`` 不是合法的往返。

拿不到 merge-base 就非零退出
----------------------------
浅克隆、缺 ``origin/main``、不在 git 仓库里——这些都让 N 无法确定。此时猜一个默认
值（哪怕「猜本文件里写的 2」）会把「深度没算对」变成「闸门变绿」，与这个脚本存在的
理由正好相反。所以失败即失败：原因写进 stderr，让 CI 变红。
"""

from __future__ import annotations

import argparse
import pathlib
import subprocess
import sys

VERSIONS_DIR = "data/migrations/versions"
DEFAULT_BASE_REF = "origin/main"
MIN_DEPTH = 1

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


class MigrationDepthError(RuntimeError):
    """往返深度无法确定。抛出即代表「不许猜」，调用方应让 CI 变红。"""


def _run_git(args: list[str], repo_root: pathlib.Path) -> str:
    """执行 git 命令并返回 stdout；任何失败都转成 :class:`MigrationDepthError`。"""
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:  # git 不在 PATH / 目录不存在
        raise MigrationDepthError(f"无法执行 git {' '.join(args)}：{exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise MigrationDepthError(
            f"git {' '.join(args)} 失败（退出码 {completed.returncode}）：{detail}"
        )
    return completed.stdout


def count_branch_migrations(name_status: str) -> int:
    """从 ``git diff --name-status`` 输出里数出本分支的迁移增量。

    纯函数：不碰磁盘、不碰 git，便于对「重命名 / 删除 / 异常状态」逐项断言。
    """
    count = 0
    deleted: list[str] = []
    for raw_line in name_status.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        fields = line.split("\t")
        status = fields[0]
        # R<score>/C<score> 有两个路径（旧 → 新），取新路径。
        if status[0] in {"R", "C"}:
            path = fields[2] if len(fields) > 2 else ""
        else:
            path = fields[1] if len(fields) > 1 else ""
        if not path.endswith(".py"):
            continue
        code = status[0]
        if code == "D":
            deleted.append(path)
        elif code in {"A", "M", "R", "C"}:
            count += 1
        else:
            # T（类型变化）/ U（未合并）等：不猜它算不算一次往返，直接失败。
            raise MigrationDepthError(f"迁移目录出现无法判定的变更状态 {status!r}：{path}")
    if deleted:
        raise MigrationDepthError(
            "迁移目录出现删除："
            + "、".join(deleted)
            + "。迁移链不允许删条目——删一条会让「已升级的库」与「新建的库」"
            "停在同一个 revision 却不同结构"
        )
    return count


def branch_merge_base(repo_root: pathlib.Path, base_ref: str) -> str:
    merge_base = _run_git(["merge-base", base_ref, "HEAD"], repo_root).strip()
    if not merge_base:
        raise MigrationDepthError(
            f"{base_ref} 与 HEAD 没有共同祖先（浅克隆？缺该远端分支？），无法确定本分支增量"
        )
    return merge_base


def branch_migration_depth(
    repo_root: pathlib.Path = _REPO_ROOT,
    base_ref: str = DEFAULT_BASE_REF,
) -> int:
    """本分支新增/改写的迁移数，下限 :data:`MIN_DEPTH`。"""
    merge_base = branch_merge_base(repo_root, base_ref)
    name_status = _run_git(
        ["diff", "--name-status", merge_base, "HEAD", "--", VERSIONS_DIR], repo_root
    )
    return max(count_branch_migrations(name_status), MIN_DEPTH)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="派生 CI 迁移往返深度（alembic downgrade -N 的 N）"
    )
    parser.add_argument(
        "--base-ref",
        default=DEFAULT_BASE_REF,
        help=f"对照基线（默认 {DEFAULT_BASE_REF}）",
    )
    parser.add_argument(
        "--repo-root",
        type=pathlib.Path,
        default=_REPO_ROOT,
        help="仓库根目录（默认本脚本所在仓库）",
    )
    args = parser.parse_args(argv)
    try:
        depth = branch_migration_depth(args.repo_root, args.base_ref)
    except MigrationDepthError as exc:
        print(f"[FAIL] 无法派生迁移往返深度：{exc}", file=sys.stderr)
        return 2
    print(depth)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
