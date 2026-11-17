"""可复现 Release 源码包构建与校验。

设计要点（F6 根治，不是补一个 zip 命令）：

1. **来源唯一**：包内容只来自 `git archive <commit>` 的受版本控制文件。
   本地 `.env`、`.venv`、缓存、`.git` 从物理上不可能进入包。
2. **双闸门**：先查「禁止入包的路径」，再扫「疑似密钥」。
   两者都在写 zip 之前执行，失败不产出包。
3. **可复现**：固定文件顺序与 ZIP 时间戳；清单记录 commit 与逐文件 SHA-256。
4. **可验证**：同一个模块提供 `--verify`，CI 用 `--check` 构建后立即验证临时包。

不新增 Manager / Policy Engine / Guard / Registry；这是单一用途的构建脚本。
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

# 同时支持 `python scripts/release_package.py` 与 `python -m scripts.release_package`。
# 直接执行时 sys.path 是 scripts/，必须显式补回仓库根，才能让两处共用同一扫描模块。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.release_scan import (
    RULES_VERSION,
    AllowlistEntry,
    Finding,
    is_text_file,
    load_allowlist,
    normalize_path,
    render_findings,
    scan_text,
    unapproved_findings,
)

MANIFEST_NAME = "RELEASE_MANIFEST.json"
_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


class ReleaseBuildError(RuntimeError):
    """发布构建失败；消息可直接给 CI / 运维阅读。"""


@dataclass(frozen=True, slots=True)
class ReleaseResult:
    """一次成功构建的可审计结果。"""

    output: Path
    package_name: str
    source_commit: str
    file_count: int


def _run_git(repo_root: Path, *args: str) -> bytes:
    """执行只读 git 命令；失败时给出可定位但不泄露内容的错误。"""
    command = ["git", "-C", str(repo_root), *args]
    result = subprocess.run(command, capture_output=True)
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise ReleaseBuildError(f"git 命令失败：{' '.join(args)}；{detail}")
    return result.stdout


def _decode_paths(raw: bytes) -> tuple[str, ...]:
    """解码 `git ... -z` 输出并统一路径口径。"""
    return tuple(normalize_path(item) for item in raw.decode("utf-8").split("\0") if item)


def resolve_commit(repo_root: Path, ref: str) -> str:
    """把任意 ref 固定成一个 commit，避免构建中途 ref 漂移。"""
    return _run_git(repo_root, "rev-parse", "--verify", f"{ref}^{{commit}}").decode().strip()


def assert_clean_worktree(repo_root: Path) -> None:
    """默认拒绝脏工作树：源码包必须能精确对应一个 commit。"""
    status = (
        _run_git(repo_root, "status", "--porcelain", "--untracked-files=normal")
        .decode("utf-8", errors="replace")
        .strip()
    )
    if status:
        lines = status.splitlines()
        preview = "\n".join(f"  {line}" for line in lines[:20])
        raise ReleaseBuildError(
            "工作树不干净，拒绝从模糊状态构建 Release；"
            f"共 {len(lines)} 项变更：\n{preview}\n"
            "请先提交，或显式传 --allow-dirty 仅用于本地排查。"
        )


def tracked_paths(repo_root: Path, commit: str) -> tuple[str, ...]:
    """列出该 commit 下所有受版本控制的文件路径。"""
    return _decode_paths(_run_git(repo_root, "ls-tree", "-r", "-z", "--name-only", commit))


def _forbidden_reason(path: str) -> str | None:
    """返回禁止入包的理由；None 表示允许。"""
    parts = tuple(PurePosixPath(path).parts)
    name = parts[-1] if parts else ""
    dirs = set(parts[:-1])

    if name == ".env" or (name.startswith(".env.") and name != ".env.example"):
        return "环境变量文件（可能含真实凭证）"
    if dirs & {".git", ".venv", "venv", "env", "dist", "build"}:
        return "版本库 / 虚拟环境 / 构建产物目录"
    if dirs & {
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".workbuddy",
        ".idea",
        ".vscode",
        "secrets",
        "credentials",
        "data_local",
    }:
        return "缓存 / 本地工具 / 密钥目录"
    if "data" in dirs and "local" in dirs:
        return "本地数据目录 data/local"
    if dirs & {"logs", "storage", "tmp", "temp"}:
        return "日志 / 本地数据 / 临时目录"
    if name in {"credentials.json", ".DS_Store", "Thumbs.db"}:
        return "凭证文件或操作系统垃圾文件"
    if PurePosixPath(name).suffix.lower() in {".pem", ".key", ".p12", ".pfx", ".log"}:
        return "私钥 / 证书 / 日志类文件"
    if PurePosixPath(name).suffix.lower() in {".pyc", ".pyo"}:
        return "Python 字节码缓存"
    return None


def assert_no_forbidden_paths(paths: Iterable[str]) -> None:
    """禁止路径检查；一次性列出全部违规项，避免逐个修的拉锯。"""
    violations = [
        (path, reason)
        for path in paths
        if (reason := _forbidden_reason(normalize_path(path))) is not None
    ]
    if violations:
        detail = "\n".join(f"  - {path}：{reason}" for path, reason in violations)
        raise ReleaseBuildError(f"发布包包含禁止入包路径：\n{detail}")


def _assert_safe_relative_path(path: str) -> None:
    """防御路径穿越；git 路径正常不会触发，但校验成本极低。"""
    pure = PurePosixPath(path)
    if pure.is_absolute() or ".." in pure.parts or not pure.parts:
        raise ReleaseBuildError(f"非法归档路径：{path}")


def _archive_files(repo_root: Path, commit: str) -> dict[str, bytes]:
    """从 git archive 取出全部文件；拒绝符号链接与特殊文件。"""
    raw = _run_git(repo_root, "archive", "--format=tar", commit)
    files: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
        for member in archive.getmembers():
            if member.isdir():
                continue
            if member.issym() or member.islnk():
                raise ReleaseBuildError(f"发布包拒绝符号链接：{member.name}")
            if not member.isfile():
                raise ReleaseBuildError(f"发布包拒绝特殊文件：{member.name}")
            rel = normalize_path(member.name)
            _assert_safe_relative_path(rel)
            extracted = archive.extractfile(member)
            if extracted is None:
                raise ReleaseBuildError(f"归档成员无法读取：{rel}")
            if rel in files:
                raise ReleaseBuildError(f"归档成员重复：{rel}")
            files[rel] = extracted.read()
    return files


def assert_archive_matches_git(files: Mapping[str, bytes], expected: Sequence[str]) -> None:
    """归档内容必须与 commit 的 tracked files 完全一致。"""
    actual = set(files)
    wanted = set(expected)
    if actual == wanted:
        return
    missing = sorted(wanted - actual)
    extra = sorted(actual - wanted)
    detail = "\n".join(
        [*(f"  缺少：{path}" for path in missing[:10]), *(f"  多出：{path}" for path in extra[:10])]
    )
    raise ReleaseBuildError(f"git archive 与 commit 文件清单不一致：\n{detail}")


def _scan_package(files: Mapping[str, bytes], allowlist: Sequence[AllowlistEntry]) -> None:
    """扫描归档内容；命中原文不写日志，只输出路径/行号/规则/指纹。"""
    findings: list[Finding] = []
    for path, data in files.items():
        if is_text_file(path, data):
            findings.extend(scan_text(path, data.decode("utf-8", errors="replace")))
    blocking = unapproved_findings(findings, allowlist)
    if blocking:
        raise ReleaseBuildError(
            "发布包密钥扫描未通过：\n"
            + render_findings(blocking, title="未放行命中")
            + "\n如确认为测试夹具，请在 release_secret_allowlist.yaml 登记指纹与理由。"
        )


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _build_manifest(
    *,
    package_name: str,
    source_ref: str,
    source_commit: str,
    files: Mapping[str, bytes],
    allowlist_bytes: bytes,
) -> bytes:
    """生成确定性的发布清单；不含时间戳，同 commit 必然同内容。"""
    manifest = {
        "schema_version": 1,
        "package_name": package_name,
        "source_ref": source_ref,
        "source_commit": source_commit,
        "rules_version": RULES_VERSION,
        "secret_scan": {
            "blocking_findings": 0,
            "allowlist_sha256": _sha256(allowlist_bytes),
        },
        "file_count": len(files),
        "files": {path: _sha256(data) for path, data in sorted(files.items())},
    }
    return (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )


def _write_zip(output: Path, prefix: str, files: Mapping[str, bytes], manifest: bytes) -> None:
    """写确定性 ZIP：顺序固定、时间戳固定、权限位规范化。"""
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=output.parent, prefix=f".{output.name}.", suffix=".tmp", delete=False
    ) as handle:
        temp_path = Path(handle.name)

    try:
        with zipfile.ZipFile(temp_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
            for rel, data in sorted(files.items()):
                info = zipfile.ZipInfo(f"{prefix}/{rel}", date_time=_ZIP_TIMESTAMP)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 3
                info.external_attr = 0o644 << 16
                zf.writestr(info, data)
            info = zipfile.ZipInfo(f"{prefix}/{MANIFEST_NAME}", date_time=_ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o644 << 16
            zf.writestr(info, manifest)
        temp_path.replace(output)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _load_manifest_from_zip(zf: zipfile.ZipFile) -> tuple[str, dict[str, str]]:
    """读取并校验发布清单，返回 (归档前缀, 文件 SHA-256 映射)。"""
    matches = [name for name in zf.namelist() if name == MANIFEST_NAME or name.endswith(f"/{MANIFEST_NAME}")]
    if len(matches) != 1:
        raise ReleaseBuildError(f"发布包必须且只能有一个 {MANIFEST_NAME}")
    manifest_path = matches[0]
    prefix = manifest_path[: -len(MANIFEST_NAME)].rstrip("/")
    if not prefix:
        raise ReleaseBuildError("发布包文件必须放在单一顶层目录内")

    raw = zf.read(manifest_path)
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseBuildError(f"{MANIFEST_NAME} 不是合法 UTF-8 JSON") from exc
    if not isinstance(manifest, dict):
        raise ReleaseBuildError(f"{MANIFEST_NAME} 顶层必须是对象")
    files = manifest.get("files")
    if not isinstance(files, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in files.items()
    ):
        raise ReleaseBuildError(f"{MANIFEST_NAME}.files 必须是 路径->sha256 映射")
    return prefix, files


def verify_release_zip(
    zip_path: Path,
    *,
    allowlist: Sequence[AllowlistEntry],
    expected_commit: str | None = None,
) -> None:
    """独立验证已有发布包：结构、禁止路径、哈希、密钥扫描。"""
    if not zip_path.is_file():
        raise ReleaseBuildError(f"发布包不存在：{zip_path}")
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            prefix, declared = _load_manifest_from_zip(zf)
            members = [name for name in zf.namelist() if not name.endswith("/")]
            forbidden = [name for name in members if _forbidden_reason(name[len(prefix) + 1 :])]
            if forbidden:
                raise ReleaseBuildError(f"发布包含禁止入包文件：{', '.join(forbidden[:10])}")

            expected_members = {f"{prefix}/{path}" for path in declared}
            expected_members.add(f"{prefix}/{MANIFEST_NAME}")
            actual_members = set(members)
            if actual_members != expected_members:
                extra = sorted(actual_members - expected_members)
                missing = sorted(expected_members - actual_members)
                raise ReleaseBuildError(
                    f"发布包成员与清单不一致；多出={extra[:5]}，缺少={missing[:5]}"
                )

            findings: list[Finding] = []
            for path, expected_hash in sorted(declared.items()):
                data = zf.read(f"{prefix}/{path}")
                if _sha256(data) != expected_hash:
                    raise ReleaseBuildError(f"发布包文件哈希不一致：{path}")
                if is_text_file(path, data):
                    findings.extend(scan_text(path, data.decode("utf-8", errors="replace")))
            blocking = unapproved_findings(findings, allowlist)
            if blocking:
                raise ReleaseBuildError(
                    "发布包密钥扫描未通过：\n" + render_findings(blocking, title="未放行命中")
                )

            if expected_commit is not None:
                raw_manifest = zf.read(f"{prefix}/{MANIFEST_NAME}")
                manifest = json.loads(raw_manifest.decode("utf-8"))
                actual_commit = manifest.get("source_commit")
                if actual_commit != expected_commit:
                    raise ReleaseBuildError(
                        f"发布包 commit 不符：expected={expected_commit}, actual={actual_commit}"
                    )
    except zipfile.BadZipFile as exc:
        raise ReleaseBuildError(f"发布包不是合法 ZIP：{zip_path}") from exc


def build_release_package(
    *,
    repo_root: Path,
    output: Path,
    allowlist: Sequence[AllowlistEntry],
    allowlist_bytes: bytes,
    ref: str = "HEAD",
    package_name: str | None = None,
    allow_dirty: bool = False,
) -> ReleaseResult:
    """构建并自验证发布包；任何闸门失败都不留下半成品。"""
    if not allow_dirty:
        assert_clean_worktree(repo_root)

    commit = resolve_commit(repo_root, ref)
    name = package_name or f"ai-creative-media-industrial-os-{commit[:12]}"
    tracked = tracked_paths(repo_root, commit)

    assert_no_forbidden_paths(tracked)
    files = _archive_files(repo_root, commit)
    assert_archive_matches_git(files, tracked)
    _scan_package(files, allowlist)

    manifest = _build_manifest(
        package_name=name,
        source_ref=ref,
        source_commit=commit,
        files=files,
        allowlist_bytes=allowlist_bytes,
    )
    _write_zip(output, name, files, manifest)
    verify_release_zip(output, allowlist=allowlist, expected_commit=commit)
    return ReleaseResult(
        output=output,
        package_name=name,
        source_commit=commit,
        file_count=len(files),
    )


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="可复现 Release 源码包构建与校验")
    parser.add_argument("--repo-root", type=Path, default=Path.cwd(), help="仓库根目录")
    parser.add_argument("--ref", default="HEAD", help="发布源 ref，默认 HEAD")
    parser.add_argument("--output", type=Path, help="输出 ZIP 路径")
    parser.add_argument("--package-name", help="发布包顶层目录名")
    parser.add_argument(
        "--allowlist",
        type=Path,
        default=None,
        help="密钥误报放行清单，默认 contracts/runtime/release_secret_allowlist.yaml",
    )
    parser.add_argument("--allow-dirty", action="store_true", help="仅本地排查：允许脏工作树")
    parser.add_argument("--check", action="store_true", help="构建到临时目录并验证，不落盘交付包")
    parser.add_argument("--verify", type=Path, help="只验证已有 ZIP")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    repo_root = args.repo_root.resolve()
    allowlist_path = args.allowlist or (
        repo_root / "contracts" / "runtime" / "release_secret_allowlist.yaml"
    )
    allowlist_bytes = allowlist_path.read_bytes()
    allowlist = load_allowlist(allowlist_bytes)

    try:
        if args.verify is not None:
            verify_release_zip(args.verify.resolve(), allowlist=allowlist)
            print(f"发布包验证通过：{args.verify}")
            return 0

        if args.check:
            with tempfile.TemporaryDirectory(prefix="release-check-") as temp_dir:
                output = Path(temp_dir) / "release.zip"
                result = build_release_package(
                    repo_root=repo_root,
                    output=output,
                    allowlist=allowlist,
                    allowlist_bytes=allowlist_bytes,
                    ref=args.ref,
                    package_name=args.package_name,
                    allow_dirty=args.allow_dirty,
                )
            print(
                f"发布检查通过：commit={result.source_commit[:12]} "
                f"files={result.file_count}（临时包未落盘）"
            )
            return 0

        if args.output is None:
            raise ReleaseBuildError("构建发布包时必须提供 --output，或使用 --check")
        result = build_release_package(
            repo_root=repo_root,
            output=args.output.resolve(),
            allowlist=allowlist,
            allowlist_bytes=allowlist_bytes,
            ref=args.ref,
            package_name=args.package_name,
            allow_dirty=args.allow_dirty,
        )
        print(
            f"发布包构建通过：{result.output} "
            f"commit={result.source_commit[:12]} files={result.file_count}"
        )
        return 0
    except ReleaseBuildError as exc:
        print(f"发布闸门失败：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
