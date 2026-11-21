"""交付包密钥扫描（生产准入条件之一，CI 门禁）。

为什么自己实现而不是只靠「看一眼」
--------------------------------
报告 P1-2 指出交付包密钥扫描未实现。密钥一旦进交付包就是不可逆泄露——
必须在 CI 里机械拦截，不能靠人记得检查。本脚本：

1. 扫描会被交付的文本文件（源码 / 配置 / 契约 / 文档 / 脚本），跳过依赖缓存与
   明确不交付的目录（.git / .venv / dist / artifacts）。
2. 用确定性规则匹配**真实密钥形状**（私钥块、云厂商 key、常见 token 前缀），
   不做「高熵猜测」——猜测会产生大量误报，误报多了就会被整体忽略。
3. 已知占位/测试夹具通过 allowlist 精确豁免，且 allowlist 每条都必须是
   「仍能在仓库中找到的原文」（陈旧豁免会报红），避免豁免清单腐烂成垃圾桶。

判据失败一律非零退出（fail-closed）：扫出疑似密钥、或 allowlist 条目失效，
都阻止合并，不放行交付。

用法：uv run python scripts/secret_scan.py
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 不交付/不扫描的目录（与 merge_readiness.IGNORE_DIRS 同口径，派生自文件系统约定）。
SKIP_DIRS = frozenset(
    {
        ".git", ".venv", "venv", "__pycache__", ".mypy_cache", ".pytest_cache",
        ".ruff_cache", "node_modules", "dist", "artifacts", "backups", ".yamltest",
    }
)

# 交付文本类型。
SCAN_SUFFIXES = frozenset(
    {".py", ".yaml", ".yml", ".json", ".toml", ".md", ".ini", ".cfg", ".env", ".sh", ".sql"}
)

# 明确允许出现在交付包里的占位——必须是「一眼假」的非真实凭证。
# 每条格式 (相对路径, 原文片段, 理由)；陈旧条目（找不到原文）会报红。
# 注：PEM 片段用常量拼接而不是整块字面量写出，避免扫描器自身的规则定义
# 被当成疑似密钥命中（自指陷阱）。拼接后 str 值与被豁免文件原文完全一致。
_PEM_BEGIN = "-----BEGIN " + "PRIVATE" + " KEY-----"
_PEM_END = "-----END " + "PRIVATE" + " KEY-----"

ALLOWLIST: tuple[tuple[str, str, str], ...] = (
    (
        "tests/unit/test_config_secrets.py",
        f"{_PEM_BEGIN}prod{_PEM_END}",
        "生产密钥校验测试夹具，值为字面 'prod'，非真实私钥",
    ),
    (
        "tests/unit/test_config_secrets.py",
        "AKIA" + "EXAMPLE",
        "AWS key 形状占位，非真实凭证",
    ),
    (
        ".env.example",
        "S3_ACCESS_KEY_ID=replace_" + "me",
        "配置样例占位值",
    ),
)


@dataclass(frozen=True)
class Rule:
    name: str
    pattern: re.Pattern[str]


# 高置信规则：宁可漏一点，也不要误报泛滥导致整体被忽略。
RULES: tuple[Rule, ...] = (
    Rule("私钥块", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    Rule("AWS Access Key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    Rule("OpenAI/API token", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    Rule("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    Rule("Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    Rule(
        "通用密钥赋值",
        re.compile(
            r"(?i)\b(secret[_-]?key|api[_-]?key|access[_-]?token|password)\s*[=:]\s*"
            r"['\"]([A-Za-z0-9+/=_\-]{24,})['\"]"
        ),
    ),
)


def iter_scan_files(root: Path):
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS or part.startswith(".") for part in path.relative_to(root).parts):
            continue
        if path.suffix not in SCAN_SUFFIXES:
            continue
        yield path


def scan(root: Path = ROOT, *, check_allowlist: bool = True) -> list[str]:
    """扫描疑似密钥；``check_allowlist`` 控制是否校验豁免条目新鲜度。

    生产入口（main）必须在真实仓库根上校验 allowlist；针对临时库的单规则负向
    测试传 ``check_allowlist=False``，因为临时库不含被豁免的仓库文件。
    """
    problems: list[str] = []

    # 1) allowlist 新鲜度：每条必须仍能在指定文件找到原文，否则豁免已腐烂。
    if check_allowlist:
        for rel, snippet, reason in ALLOWLIST:
            target = root / rel
            if not target.exists() or snippet not in target.read_text(encoding="utf-8", errors="ignore"):
                problems.append(
                    f"[FAIL] 密钥豁免已失效（陈旧豁免不得静默保留）：{rel} 找不到 {snippet[:40]!r}；理由={reason}"
                )

    allowed = {(rel, snippet) for rel, snippet, _ in ALLOWLIST}

    # 2) 逐文件扫描高置信规则。
    for path in iter_scan_files(root):
        rel = path.relative_to(root).as_posix()
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            for rule in RULES:
                if not rule.pattern.search(line):
                    continue
                if any(rel == a_rel and a_snip in line for a_rel, a_snip in allowed):
                    continue
                problems.append(
                    f"[FAIL] 疑似密钥：{rel}:{lineno} 命中规则「{rule.name}」：{line.strip()[:120]}"
                )
    return problems


def main() -> int:
    problems = scan(ROOT)
    if problems:
        for p in problems:
            print(p)
        print(f"[FAIL] 交付包密钥扫描未通过：{len(problems)} 项；交付前必须清除或登记有据豁免")
        return 1
    print(
        f"[PASS] 交付包密钥扫描通过：{len(RULES)} 条高置信规则 / "
        f"{len(ALLOWLIST)} 条可机检豁免，无命中"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
