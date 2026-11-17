"""发布包密钥扫描（纯策略层）。

职责边界：

- 只回答「这段文本里有没有疑似密钥」，不读命令行、不碰 git、不写文件；
- 匹配值永不出现在 Findings 的字符串表示里，只保留指纹（SHA-256 前 12 位）；
- 已知测试夹具通过显式 allowlist 放行，放行必须写路径 + 规则 + 指纹 + 理由。

这样发布脚本和单元测试可以共用同一套规则，不会产生第二套扫描口径。
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, cast

import yaml


@dataclass(frozen=True, slots=True)
class Rule:
    """一条密钥识别规则。"""

    id: str
    description: str
    pattern: re.Pattern[str]
    value_group: str | int = 0


# 规则版本号进入 RELEASE_MANIFEST.json。规则变更必须升版本，
# 否则「同一个发布包在不同时间扫出不同结论」会变成无法解释的偶发失败。
RULES_VERSION = "release-scan-v1"

RULES: tuple[Rule, ...] = (
    Rule(
        id="private_key_block",
        description="PEM 私钥块",
        pattern=re.compile(
            r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----"
        ),
    ),
    Rule(
        id="aws_access_key_id",
        description="AWS access key id",
        pattern=re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    ),
    Rule(
        id="github_token",
        description="GitHub personal / fine-grained token",
        pattern=re.compile(
            r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{40,})\b"
        ),
    ),
    Rule(
        id="slack_token",
        description="Slack bot / user token",
        pattern=re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    ),
    Rule(
        id="openai_api_key",
        description="OpenAI 风格 API key",
        pattern=re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b"),
    ),
    Rule(
        id="jwt_compact",
        description="JWT 紧凑序列化串",
        pattern=re.compile(
            r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"
        ),
    ),
    Rule(
        id="assigned_credential",
        description="行首赋值形式的长凭据（非占位值）",
        pattern=re.compile(
            r"(?im)^[ \t]*(?:export[ \t]+)?"
            r"(?P<key>[A-Z0-9_]*(?:SECRET|TOKEN|PASSWORD|PASSWD|API_KEY|"
            r"ACCESS_KEY|PRIVATE_KEY|CREDENTIAL)[A-Z0-9_]*)"
            r"[ \t]*[:=][ \t]*(?P<quote>[\"']?)"
            r"(?P<value>[^\s\"'#]{16,})(?P=quote)"
        ),
        value_group="value",
    ),
    Rule(
        id="url_credentials",
        description="URL 内嵌用户名密码",
        pattern=re.compile(r"\b[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s@]{8,}@"),
    ),
)

# 明显的占位值不需要靠 allowlist 放行；allowlist 只用于「看起来像真密钥、
# 但已确认是测试夹具」的极少数情况，避免它退化成默认忽略清单。
_PLACEHOLDER_RE = re.compile(
    r"(?i)^(?:change[-_]?me|changeme|example|placeholder|dummy|todo|none|null|"
    r"xxx+|your[-_]|test(?:[-_]|$)|localhost|postgres|admin|root|"
    r"app_dev_password|sk-model|s3cret[-_]?\w*)$"
)

_ASSIGNMENT_SKIP_KEY_SUFFIXES = (
    "_REF",
    "_ID",
    "_PATH",
    "_FILE",
    "_ENV",
    "_CLASS",
    "_NAME",
    "_TYPE",
    "_CREDENTIALS",
)

# 未加引号的 RHS 若含这些字符，几乎一定是代码表达式/插值，而不是字面量凭据。
_ASSIGNMENT_CODE_CHARS = frozenset("(){}[]$.|,<>")

_BINARY_SUFFIXES = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".bmp",
        ".ico",
        ".pdf",
        ".zip",
        ".gz",
        ".tar",
        ".woff",
        ".woff2",
        ".ttf",
        ".otf",
        ".so",
        ".dll",
        ".exe",
        ".pyc",
        ".pyo",
    }
)


@dataclass(frozen=True, slots=True)
class Finding:
    """一条疑似密钥命中；刻意不保存原文。"""

    path: str
    line: int
    rule_id: str
    fingerprint: str

    def __str__(self) -> str:
        return (
            f"{self.path}:{self.line} [{self.rule_id}] "
            f"fingerprint={self.fingerprint}"
        )


@dataclass(frozen=True, slots=True)
class AllowlistEntry:
    """一条显式、可审计的误报放行。"""

    path: str
    rule_id: str
    fingerprint: str
    reason: str

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.path, self.rule_id, self.fingerprint)


def normalize_path(path: str) -> str:
    """把路径统一成 POSIX 相对路径，供规则与 allowlist 使用。"""
    normalized = PurePosixPath(path).as_posix()
    return normalized[2:] if normalized.startswith("./") else normalized


def fingerprint(value: str) -> str:
    """对命中值取稳定短指纹；日志和清单里只出现这个，不出现明文。"""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def is_text_file(path: str, data: bytes) -> bool:
    """按后缀 + NUL 字节粗判是否文本；二进制不进入正则扫描。"""
    if PurePosixPath(path).suffix.lower() in _BINARY_SUFFIXES:
        return False
    return b"\0" not in data[:8192]


def _is_assignment_false_positive(match: re.Match[str], value: str) -> bool:
    """排除代码赋值/模板插值/引用字段，避免 allowlist 退化成常规忽略清单。"""
    key = match.group("key").upper()
    if key.endswith(_ASSIGNMENT_SKIP_KEY_SUFFIXES) or key in {"CREDENTIALS", "SECRETS"}:
        return True
    quote = match.group("quote")
    if quote:
        return False
    return any(char in value for char in _ASSIGNMENT_CODE_CHARS)


def scan_text(path: str, text: str) -> tuple[Finding, ...]:
    """扫描单个文本文件，返回全部命中（含后续可被 allowlist 放行的项）。"""
    rel = normalize_path(path)
    findings: list[Finding] = []
    for rule in RULES:
        for match in rule.pattern.finditer(text):
            value = (
                match.group(rule.value_group)
                if isinstance(rule.value_group, str)
                else match.group(0)
            )
            if not value or _PLACEHOLDER_RE.match(value):
                continue
            if rule.id == "assigned_credential" and _is_assignment_false_positive(match, value):
                continue
            line = text.count("\n", 0, match.start()) + 1
            findings.append(
                Finding(
                    path=rel,
                    line=line,
                    rule_id=rule.id,
                    fingerprint=fingerprint(value),
                )
            )
    return tuple(findings)


def scan_files(files: Iterable[tuple[str, bytes]]) -> tuple[Finding, ...]:
    """扫描 (相对路径, 内容) 序列；只处理文本文件。"""
    findings: list[Finding] = []
    for path, data in files:
        if not is_text_file(path, data):
            continue
        findings.extend(scan_text(path, data.decode("utf-8", errors="replace")))
    return tuple(findings)


def load_allowlist(data: bytes) -> tuple[AllowlistEntry, ...]:
    """解析 allowlist YAML；结构错误直接拒绝，避免静默放行。"""
    raw = cast(dict[str, Any], yaml.safe_load(data) or {})
    entries = raw.get("entries", [])
    if not isinstance(entries, list):
        raise ValueError("release secret allowlist: entries 必须是列表")

    result: list[AllowlistEntry] = []
    seen: set[tuple[str, str, str]] = set()
    for index, item in enumerate(entries):
        if not isinstance(item, dict):
            raise ValueError(f"release secret allowlist: 第 {index} 项必须是映射")
        reason = str(item.get("reason", "")).strip()
        if not reason:
            raise ValueError(f"release secret allowlist: 第 {index} 项缺 reason")
        entry = AllowlistEntry(
            path=normalize_path(str(item.get("path", ""))),
            rule_id=str(item.get("rule_id", "")),
            fingerprint=str(item.get("fingerprint", "")).strip().lower(),
            reason=reason,
        )
        if not entry.path or not entry.rule_id or len(entry.fingerprint) != 12:
            raise ValueError(f"release secret allowlist: 第 {index} 项字段不完整")
        if entry.key in seen:
            raise ValueError(f"release secret allowlist: 重复条目 {entry.key}")
        seen.add(entry.key)
        result.append(entry)
    return tuple(result)


def unapproved_findings(
    findings: Iterable[Finding], allowlist: Iterable[AllowlistEntry]
) -> tuple[Finding, ...]:
    """过滤掉已登记误报；未放行的命中就是发布阻断项。"""
    approved = {entry.key for entry in allowlist}
    return tuple(f for f in findings if (f.path, f.rule_id, f.fingerprint) not in approved)


def render_findings(findings: Iterable[Finding], *, title: str) -> str:
    """渲染人类可读报告，不输出任何命中原文。"""
    items = tuple(findings)
    if not items:
        return f"{title}: 0 项"
    body = "\n".join(f"  - {finding}" for finding in items)
    return f"{title}: {len(items)} 项\n{body}"


def findings_by_rule(findings: Iterable[Finding]) -> Mapping[str, int]:
    """按规则汇总数量，供发布清单登记。"""
    counts: dict[str, int] = {}
    for finding in findings:
        counts[finding.rule_id] = counts.get(finding.rule_id, 0) + 1
    return counts
