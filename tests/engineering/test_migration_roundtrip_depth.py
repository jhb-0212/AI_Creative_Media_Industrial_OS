"""迁移往返深度必须**派生**，不能手抄（``alembic downgrade -N`` 的 N）。

来历：CI 里的 N 被两次写小。``-1`` 时代漏掉 ``c5d6e7f8a9b0`` 的分区撤销；到
2026-11-25，本分支新增 3 个迁移、CI 仍写 ``-2``，最早那个（``a7b8c9d0e1f2``）的
降级从未在 CI 执行过。两次 CI 都是绿的——**写死的常量在这里不是判据，是装饰**。

这些用例刻意分成两层：

- 解析层：A/M/D/R/异常状态各一条，确保「数什么、不算什么」是明确的；
- 门禁层：workflow 若被改回字面量 ``downgrade -2``，这条会红。没有这一层，
  脚本可以存在得很好，而 CI 继续用旧常量。
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess

import pytest

from scripts import migration_roundtrip_depth as depth

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_CI = _REPO_ROOT / ".github" / "workflows" / "ci.yml"
_GIT = shutil.which("git")


def test_added_and_modified_are_counted() -> None:
    output = (
        "A\tdata/migrations/versions/aaaa_first.py\n"
        "M\tdata/migrations/versions/bbbb_second.py\n"
    )
    assert depth.count_branch_migrations(output) == 2


def test_non_python_files_in_versions_dir_are_ignored() -> None:
    output = "A\tdata/migrations/versions/README.md\nA\tdata/migrations/versions/cccc_third.py\n"
    assert depth.count_branch_migrations(output) == 1


def test_rename_counts_once_on_new_path() -> None:
    output = "R100\tdata/migrations/versions/old_name.py\tdata/migrations/versions/dddd_new.py\n"
    assert depth.count_branch_migrations(output) == 1


def test_deleted_migration_fails_closed_and_names_the_file() -> None:
    output = "D\tdata/migrations/versions/eeee_gone.py\n"
    with pytest.raises(depth.MigrationDepthError, match=r"eeee_gone\.py"):
        depth.count_branch_migrations(output)


def test_unknown_status_fails_closed_instead_of_guessing() -> None:
    output = "T\tdata/migrations/versions/ffff_retyped.py\n"
    with pytest.raises(depth.MigrationDepthError, match="无法判定"):
        depth.count_branch_migrations(output)


def test_missing_merge_base_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """取不到共同祖先时必须抛错——猜默认值等于让闸门变绿。"""

    def fake_run_git(args: list[str], repo_root: pathlib.Path) -> str:
        raise depth.MigrationDepthError(f"git {' '.join(args)} 失败（退出码 128）")

    monkeypatch.setattr(depth, "_run_git", fake_run_git)
    with pytest.raises(depth.MigrationDepthError):
        depth.branch_migration_depth(_REPO_ROOT, "origin/does-not-exist")


def test_empty_diff_still_round_trips_one_step(monkeypatch: pytest.MonkeyPatch) -> None:
    """增量为 0（例如在 main 上跑）时返回下限 1：``-0`` 不是合法往返。"""

    def fake_run_git(args: list[str], repo_root: pathlib.Path) -> str:
        if args[0] == "merge-base":
            return "0" * 40 + "\n"
        return ""

    monkeypatch.setattr(depth, "_run_git", fake_run_git)
    assert depth.branch_migration_depth(_REPO_ROOT, "origin/main") == depth.MIN_DEPTH
    assert depth.MIN_DEPTH >= 1


def test_ci_uses_derived_depth_not_a_literal() -> None:
    """防回归：workflow 必须调派生脚本，且不得再出现字面量 ``downgrade -<n>``。"""
    text = _CI.read_text(encoding="utf-8")
    assert "scripts/migration_roundtrip_depth.py" in text
    assert 'alembic downgrade "-${DEPTH}"' in text
    for literal in (
        "alembic downgrade -1",
        "alembic downgrade -2",
        "alembic downgrade -3",
    ):
        assert literal not in text, f"CI 又把往返深度写死了：{literal}"


@pytest.mark.skipif(_GIT is None, reason="环境中没有 git")
def test_real_repository_counts_only_branch_additions(tmp_path: pathlib.Path) -> None:
    """用真 git 仓库验证 merge-base 与 path 过滤：只数本分支新增，不数基线已有的。"""
    repo = tmp_path / "repo"
    versions = repo / depth.VERSIONS_DIR
    versions.mkdir(parents=True)

    def git(*args: str) -> None:
        subprocess.run(
            ["git", "-c", "commit.gpgsign=false", *args],
            cwd=repo,
            check=True,
            capture_output=True,
        )

    git("init", "-b", "main")
    git("config", "user.name", "ci")
    git("config", "user.email", "ci@example.invalid")
    (versions / "base_one.py").write_text("revision = 'base_one'\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-m", "base: 1 migration")
    git("checkout", "-b", "feature")
    (versions / "new_two.py").write_text("revision = 'new_two'\n", encoding="utf-8")
    (versions / "new_three.py").write_text("revision = 'new_three'\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-m", "feature: 2 migrations")

    assert depth.branch_migration_depth(repo, base_ref="main") == 2
