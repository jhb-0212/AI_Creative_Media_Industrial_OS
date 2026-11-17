"""F6 发布包治理的单测：禁止路径、密钥扫描、可复现构建与验证。

这些测试刻意不依赖仓库当前文件清单（临时 git 仓库自建），
避免「今天通过、明天加一个文件就失败」的脆弱断言。
"""

from __future__ import annotations

import json
import subprocess
import zipfile
from pathlib import Path

import pytest

from scripts.release_package import (
    MANIFEST_NAME,
    ReleaseBuildError,
    assert_clean_worktree,
    assert_no_forbidden_paths,
    build_release_package,
    verify_release_zip,
)
from scripts.release_scan import (
    AllowlistEntry,
    fingerprint,
    load_allowlist,
    scan_text,
    unapproved_findings,
)

_EMPTY_ALLOWLIST = b"schema_version: 1\nentries: []\n"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _make_repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "README.md").write_text("release fixture\n", encoding="utf-8")
    (repo / ".env.example").write_text("POSTGRES_HOST=localhost\n", encoding="utf-8")
    _git(repo, "add", "README.md", ".env.example")
    _git(
        repo,
        "-c",
        "user.email=release-test@example.com",
        "-c",
        "user.name=release-test",
        "commit",
        "-q",
        "-m",
        "fixture",
    )
    return repo


def test_normalize_path_keeps_dotfile_prefix() -> None:
    """`.env` 不能被路径归一化误改成 `env`，否则禁止路径闸门会失效。"""
    from scripts.release_scan import normalize_path

    assert normalize_path(".env") == ".env"
    assert normalize_path("./.env.production") == ".env.production"


def test_forbidden_paths_detected_in_one_pass() -> None:
    """禁止入包路径一次性列全，并且 `.env.example` 仍然允许。"""
    with pytest.raises(ReleaseBuildError) as exc:
        assert_no_forbidden_paths(
            [
                "app/.env",
                "app/.venv/lib/x.py",
                "app/.git/config",
                "app/data/local/cache.json",
                "app/credentials.json",
                "app/README.md",
            ]
        )
    message = str(exc.value)
    for needle in (".env", ".venv", ".git", "data/local", "credentials.json"):
        assert needle in message
    assert "README.md" not in message

    assert_no_forbidden_paths(["app/.env.example"])


def test_secret_scan_detects_real_patterns_but_not_code_expressions() -> None:
    """扫描要抓真密钥，不能把变量引用/模板插值当密钥。"""
    text = "\n".join(
        [
            "AWS_KEY = 'AKIA1234567890ABCDEF'",
            "PRIVATE_KEY = '-----BEGIN PRIVATE KEY-----'",
            "POSTGRES_PASSWORD = get_settings().postgres_password.get_secret_value()",
            "OUTBOX_WEBHOOK_SECRET: ${OUTBOX_WEBHOOK_SECRET:-}",
            "credential_ref: cred_project_x_provider_y",
        ]
    )
    findings = scan_text("fixture.py", text)
    assert {item.rule_id for item in findings} == {"aws_access_key_id", "private_key_block"}
    assert all("AKIA" not in str(item) for item in findings)


def test_allowlist_requires_exact_path_rule_and_fingerprint() -> None:
    """放行必须是精确三元组；路径或规则变了就要重新确认。"""
    token = "AKIA1234567890ABCDEF"
    digest = fingerprint(token)
    entries = load_allowlist(
        f"""
schema_version: 1
entries:
  - path: tests/fixture.py
    rule_id: aws_access_key_id
    fingerprint: {digest}
    reason: fixture
""".encode()
    )
    assert entries == (
        AllowlistEntry(
            path="tests/fixture.py",
            rule_id="aws_access_key_id",
            fingerprint=digest,
            reason="fixture",
        ),
    )

    findings = scan_text("tests/fixture.py", token)
    assert len(findings) == 1
    assert unapproved_findings(findings, entries) == ()
    assert len(unapproved_findings(findings, ())) == 1

    moved = scan_text("other/fixture.py", "AKIA1234567890ABCDEF")
    assert len(unapproved_findings(moved, entries)) == 1


def test_allowlist_rejects_entry_without_reason() -> None:
    """无理由放行等于静默忽略，必须拒绝加载。"""
    with pytest.raises(ValueError, match="reason"):
        load_allowlist(
            b"""
schema_version: 1
entries:
  - path: tests/fixture.py
    rule_id: aws_access_key_id
    fingerprint: abcdef123456
"""
        )


def test_build_is_deterministic_and_excludes_untracked_env(tmp_path: Path) -> None:
    """同 commit 两次构建字节一致；本地 `.env` 不进包。"""
    repo = _make_repo(tmp_path)
    (repo / ".env").write_text("MODEL_GATEWAY_API_KEY=sk-this-is-a-local-secret\n", encoding="utf-8")

    first = build_release_package(
        repo_root=repo,
        output=tmp_path / "dist" / "first.zip",
        allowlist=(),
        allowlist_bytes=_EMPTY_ALLOWLIST,
        allow_dirty=True,
    )
    second = build_release_package(
        repo_root=repo,
        output=tmp_path / "dist" / "second.zip",
        allowlist=(),
        allowlist_bytes=_EMPTY_ALLOWLIST,
        allow_dirty=True,
    )

    assert first.output.read_bytes() == second.output.read_bytes()
    with zipfile.ZipFile(first.output) as zf:
        names = zf.namelist()
        assert all(name != f"{first.package_name}/.env" for name in names)
        assert f"{first.package_name}/.env.example" in names
        manifest = json.loads(zf.read(f"{first.package_name}/{MANIFEST_NAME}"))
        assert manifest["source_commit"] == first.source_commit
        assert manifest["file_count"] == 2

    verify_release_zip(first.output, allowlist=(), expected_commit=first.source_commit)


def test_clean_worktree_is_required_by_default(tmp_path: Path) -> None:
    """脏工作树默认拒绝，避免交付包与声称的 commit 不一致。"""
    repo = _make_repo(tmp_path)
    (repo / "untracked.txt").write_text("not committed\n", encoding="utf-8")
    with pytest.raises(ReleaseBuildError, match="工作树不干净"):
        assert_clean_worktree(repo)


def test_verify_detects_tampered_file(tmp_path: Path) -> None:
    """清单哈希能发现包内文件被替换。"""
    repo = _make_repo(tmp_path)
    result = build_release_package(
        repo_root=repo,
        output=tmp_path / "release.zip",
        allowlist=(),
        allowlist_bytes=_EMPTY_ALLOWLIST,
    )
    tampered = tmp_path / "tampered.zip"
    with zipfile.ZipFile(result.output) as source, zipfile.ZipFile(tampered, "w") as target:
        for item in source.infolist():
            data = source.read(item.filename)
            if item.filename.endswith("/README.md"):
                data = b"tampered\n"
            target.writestr(item, data)

    with pytest.raises(ReleaseBuildError, match="哈希不一致"):
        verify_release_zip(tampered, allowlist=(), expected_commit=result.source_commit)


def test_fingerprint_never_contains_source_value() -> None:
    """指纹只保留摘要，不能把密钥原文带进日志。"""
    value = "AKIA1234567890ABCDEF"
    digest = fingerprint(value)
    assert len(digest) == 12
    assert value not in digest
