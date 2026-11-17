"""A1 证据生成：运行时可复现清单（Phase 1 验收 A1）。

**A1 要的三样东西**

> 镜像摘要、依赖锁文件、配置清单 —— 从空环境按清单可重建出同一版本，
> 版本号与基线一致（V2.0.5）。

三样里"配置清单"最容易变成过期文档：人手写一份环境变量表，第二天加了个字段
就没人回头改。所以这里**由代码生成**，并配一条单测（`tests/unit/
test_runtime_inventory.py`）断言磁盘上的文档与当前 `Settings` 逐行一致——
文档一过期测试就红，不依赖人的自觉。

**为什么默认值要区分"密钥"与"普通值"**

`SecretStr` 字段的默认值不打印（只标注"不回显"）。清单会进仓库与发布附件，
把默认口令写进文档等于把 `app_dev_password` 再抄一遍到更多地方。仍然保留
"是否敏感"这一列，因为运维需要知道哪些值必须由密钥系统注入。

**为什么基础镜像要标"是否按 digest 固定"**

`FROM image:tag` 里的 tag 是可变的：同一个 Dockerfile 今天和明天可能构建出
两个不同的运行时。A1 的"重建出同一版本"在 tag 未固定时**不可能成立**。
清单把这一列显式列出来，就是为了让这个缺陷可见，而不是藏在 Dockerfile 里。

用法：

    python scripts/runtime_inventory.py            # 打印到 stdout
    python scripts/runtime_inventory.py --write    # 写到 docs/evidence/phase1-runtime-inventory.md
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path

from pydantic import SecretStr

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.shared.config import Settings, get_settings  # noqa: E402

DEFAULT_OUTPUT = ROOT / "docs" / "evidence" / "phase1-runtime-inventory.md"

_DOCKERFILE = ROOT / "infra" / "docker" / "Dockerfile.api"
_UV_LOCK = ROOT / "uv.lock"
_PYPROJECT = ROOT / "pyproject.toml"
_ALEMBIC_VERSIONS = ROOT / "data" / "migrations" / "versions"

_FROM_RE = re.compile(r"^FROM\s+(\S+)(?:\s+AS\s+(\S+))?", re.IGNORECASE)
_ARG_VERSION_RE = re.compile(r"^ARG\s+APP_VERSION=(\S+)", re.MULTILINE)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _mask_default(value: object) -> str:
    """默认值的展示形态：密钥只报"是否为空"，永不回显内容。"""
    if isinstance(value, SecretStr):
        return "（密钥，默认空）" if not value.get_secret_value() else "（密钥，不回显）"
    if isinstance(value, str):
        return f"`{value}`" if value else "（空字符串）"
    return f"`{value}`"


def _field_type(field: object) -> str:
    """字段类型名。

    ``SecretStr`` 要按默认值判定而不是看注解：pydantic 会把它解析成
    ``Annotated[SecretStr, ...]``，``annotation.__name__`` 取不到可读名字
    （实测得到 ``Str``，会误导运维以为那是普通字符串）。
    """
    default = getattr(field, "default", None)
    if isinstance(default, SecretStr):
        return "SecretStr"
    annotation = getattr(field, "annotation", None)
    text = getattr(annotation, "__name__", None) or str(annotation)
    return text.replace("typing.", "")


def _production_required(settings_cls: type[Settings]) -> dict[str, str]:
    """生产必填字段（字段名 → 环境变量名）。取自代码里的同一份常量。

    pydantic 会把下划线开头的类属性包成 ``ModelPrivateAttr``（防止误当字段），
    所以要先取 ``.default`` 再迭代；这一步不做，脚本会报"不可迭代"而不是
    "常量不存在"，排障方向会被误导。
    """
    raw = getattr(settings_cls, "_PRODUCTION_REQUIRED_SECRETS", None)
    if raw is None:
        return {}
    value = getattr(raw, "default", raw)
    return dict(value)


def _validators(settings_cls: type[Settings]) -> list[tuple[str, str]]:
    """收集 model validator 及其首行说明（pydantic v2 的装饰器表）。"""
    collected: list[tuple[str, str]] = []
    decorators = getattr(settings_cls, "__pydantic_decorators__", None)
    if decorators is None:  # pragma: no cover - pydantic 版本变化时的兜底
        return collected
    for name in decorators.model_validators:
        func = getattr(settings_cls, name, None)
        doc = (func.__doc__ or "").strip().splitlines()
        collected.append((name, doc[0].strip() if doc else "（无说明）"))
    return sorted(collected)


def _base_images() -> list[tuple[str, str, bool]]:
    """解析 Dockerfile 的 FROM：(镜像引用, 阶段名, 是否按 digest 固定)。"""
    images: list[tuple[str, str, bool]] = []
    for line in _DOCKERFILE.read_text(encoding="utf-8").splitlines():
        match = _FROM_RE.match(line.strip())
        if match is None:
            continue
        reference = match.group(1)
        stage = match.group(2) or "（默认）"
        images.append((reference, stage, "@sha256:" in reference))
    return images


def _migration_count() -> int:
    return len(list(_ALEMBIC_VERSIONS.glob("*.py")))


def dockerfile_app_version() -> str | None:
    """Dockerfile 里 ``ARG APP_VERSION`` 的值（镜像 OCI 版本标签的来源）。

    单测会拿它与 ``pyproject.toml`` 的 version 对比：不写死 version 标签的镜像
    会继承基础镜像的标签（实测为 uv 的 ``0.9.30-...``），制品库里会被认错版本，
    所以两处必须由同一断言钉住。
    """
    match = _ARG_VERSION_RE.search(_DOCKERFILE.read_text(encoding="utf-8"))
    return match.group(1).strip() if match else None


def render() -> str:
    """渲染清单（**确定性输出**：不含时间戳/commit，否则单测无法比对）。"""
    settings = get_settings()
    settings_cls = Settings
    prod_required = _production_required(settings_cls)

    lines: list[str] = [
        "# 运行时可复现清单（Phase 1 验收 A1）",
        "",
        "> 本文件由 `scripts/runtime_inventory.py` 生成，**请勿手工编辑**。",
        "> `tests/unit/test_runtime_inventory.py` 会断言它与当前代码逐行一致——",
        "> 手改会在下一次测试里被覆盖，加字段后忘记重新生成则会测试失败。",
        "",
        "## 1. 版本基线",
        "",
        "| 项 | 值 | 来源 |",
        "|---|---|---|",
        f"| 平台版本 | `{settings.app_version}` | `Settings.app_version`（契约 V2.0.5） |",
        f"| Python 要求 | `{_python_requirement()}` | `pyproject.toml` |",
        f"| 依赖锁文件 | `uv.lock`，sha256 `{_sha256(_UV_LOCK)}`（{_UV_LOCK.stat().st_size} 字节） | 仓库 |",
        f"| 项目定义 | `pyproject.toml`，sha256 `{_sha256(_PYPROJECT)}` | 仓库 |",
        f"| 迁移脚本 | {_migration_count()} 个 | `data/migrations/versions/` |",
        f"| 镜像版本标签 | `{dockerfile_app_version() or '（未声明）'}` | "
        "`Dockerfile.api` 的 `ARG APP_VERSION`（写入 OCI `image.version` 标签） |",
        "",
        "## 2. 基础镜像",
        "",
        "| 镜像引用 | 阶段 | 按 digest 固定 |",
        "|---|---|---|",
    ]
    for reference, stage, pinned in _base_images():
        lines.append(f"| `{reference}` | {stage} | {'是' if pinned else '**否**'} |")

    # 同一镜像可能被多个阶段引用（builder 与 runtime 必须同源，见 Dockerfile
    # 注释 1），去重后只报一次，否则同一缺陷会在清单里出现两遍。
    unpinned = list(dict.fromkeys(ref for ref, _, pinned in _base_images() if not pinned))
    lines += [
        "",
        (
            "全部基础镜像已按 digest 固定，重建时基础层字节一致。"
            if not unpinned
            else "**未固定 digest 的基础镜像**："
            + "、".join(f"`{ref}`" for ref in unpinned)
            + "。tag 可变意味着同一 Dockerfile 在不同时间可能构建出不同运行时，"
            "A1 的「按清单重建出同一版本」在此项被固定前不成立。"
        ),
        "",
        "## 3. 配置项（环境变量）",
        "",
        "变量名为字段名大写。`敏感` 列表示该值按密钥处理（回显与日志均脱敏）；",
        "`生产必填` 列表示 `APP_ENV=production` 下为空则拒绝启动。",
        "",
        "| 环境变量 | 类型 | 默认值 | 敏感 | 生产必填 |",
        "|---|---|---|---|---|",
    ]
    for name, field in settings_cls.model_fields.items():
        # 默认值取**代码里的 default**，不取当前环境实例化的值：清单是
        # "从空环境重建"的说明书，若按 `.env` 渲染，同一份文档在不同机器上
        # 内容不同，A1 的可复现性反而被这份证据自己破坏。
        default = field.default
        secret = isinstance(default, SecretStr)
        required = "是" if name in prod_required else ""
        lines.append(
            f"| `{name.upper()}` | {_field_type(field)} | "
            f"{_mask_default(default)} | {'是' if secret else ''} | {required} |"
        )

    lines += [
        "",
        "## 4. 启动期校验（配置错误一律拒绝启动，不留到运行期）",
        "",
        "| 校验 | 说明 |",
        "|---|---|",
    ]
    for name, summary in _validators(settings_cls):
        lines.append(f"| `{name}` | {summary} |")

    lines += [
        "",
        "## 5. 重建步骤（从空环境的唯一入口）",
        "",
        "```bash",
        "uv sync --frozen --extra dev          # 严格按 uv.lock，漂移即失败",
        "python -m alembic upgrade head        # 建库结构（当前 head 见迁移目录最大 revision）",
        "python -m uvicorn apps.api.main:app   # 或 docker compose -f infra/compose/docker-compose.yml up",
        "```",
        "",
        "容器构建：`docker build -f infra/docker/Dockerfile.api -t ai-film-industrial-api:<tag> .`",
        "",
    ]
    return "\n".join(lines)


def _python_requirement() -> str:
    text = _PYPROJECT.read_text(encoding="utf-8")
    match = re.search(r'requires-python\s*=\s*"([^"]+)"', text)
    return match.group(1) if match else "（未声明）"


def package_version() -> str | None:
    """``pyproject.toml`` 的 version（与 ``Settings.app_version`` 由测试对齐）。"""
    text = _PYPROJECT.read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    return match.group(1) if match else None


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 A1 运行时可复现清单")
    parser.add_argument("--write", action="store_true", help=f"写入 {DEFAULT_OUTPUT}")
    args = parser.parse_args()

    content = render()
    if args.write:
        DEFAULT_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        DEFAULT_OUTPUT.write_text(content, encoding="utf-8")
        print(f"已写入：{DEFAULT_OUTPUT}（{len(content.splitlines())} 行）")
    else:
        print(content)
    return 0


if __name__ == "__main__":
    sys.exit(main())
