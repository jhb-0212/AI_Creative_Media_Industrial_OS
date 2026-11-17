"""A1 运行时可复现清单的防呆测试。

**为什么这类"文档"需要测试**

`docs/evidence/phase1-runtime-inventory.md` 是 A1 的证据，由
`scripts/runtime_inventory.py` 从 `Settings` 与 `Dockerfile` 生成。它最可能的
失效方式不是写错，而是**悄悄过期**：加一个配置字段、改一个基础镜像，文档没人
回头改，验收时拿着一份与代码不符的清单去证明"环境可复现"。

所以这里不让它过期——生成结果与磁盘文件不一致就红。四条断言各自守一个失败面：

1. 文件 == 当前代码的渲染结果（过期即红）；
2. 每个 `Settings` 字段都出现在清单里（新增字段不会漏记）；
3. 密钥默认值**绝不出现在**清单文本里（清单会进仓库与发布附件）；
4. 每个 `FROM` 都按 digest 固定（tag 可变则"重建同一版本"不成立）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from pydantic import SecretStr

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import runtime_inventory as inv  # noqa: E402

from packages.shared.config import Settings  # noqa: E402


@pytest.fixture(scope="module")
def rendered() -> str:
    return inv.render()


def test_document_is_in_sync_with_code(rendered: str) -> None:
    """磁盘上的清单必须等于当前代码的渲染结果。

    失败时的修法只有一条：重跑 ``python scripts/runtime_inventory.py --write``。
    手改文档会在下一次测试被覆盖，这正是"生成物不入手工编辑"的意思。
    """
    assert inv.DEFAULT_OUTPUT.exists(), (
        f"缺少 {inv.DEFAULT_OUTPUT}；请运行 python scripts/runtime_inventory.py --write"
    )
    on_disk = inv.DEFAULT_OUTPUT.read_text(encoding="utf-8")
    assert on_disk == rendered, (
        "A1 清单已过期：重跑 python scripts/runtime_inventory.py --write"
    )


def test_every_settings_field_is_listed(rendered: str) -> None:
    """新增配置字段必须出现在清单里。

    只断言"字段名出现在文本中"是刻意的弱断言：清单的排版可以变，但漏掉一个
    环境变量就意味着部署方不知道要配它。
    """
    for name in Settings.model_fields:
        assert f"`{name.upper()}`" in rendered, f"配置项 {name.upper()} 未进清单"


def test_secret_defaults_are_never_printed(rendered: str) -> None:
    """密钥字段的行不得出现任何口令内容，只允许"默认空/不回显"。

    按**行**断言而不是全文子串：全文里 ``postgres`` 这种词本来就会出现
    （用户名默认值、DSN 片段），子串检查会把正常内容报成泄露。按行检查的
    语义才是真的——"那一格有没有把口令写出来"。

    同时校验清单记录的是**代码里的默认值**，不是当前环境的实际值：后者会让
    这份 A1 证据随 `.env` 漂移，反而破坏"可复现"。
    """
    for name, field in Settings.model_fields.items():
        default = field.default
        if not isinstance(default, SecretStr):
            continue
        row = next(
            (line for line in rendered.splitlines() if line.startswith(f"| `{name.upper()}` |")),
            None,
        )
        assert row is not None, f"密钥字段 {name.upper()} 未出现在清单中"
        assert "（密钥，" in row, f"{name.upper()} 未按密钥标注：{row}"
        secret = default.get_secret_value()
        if secret:
            assert secret not in row, f"{name.upper()} 的默认口令被写进了清单"


def test_all_base_images_are_digest_pinned(rendered: str) -> None:
    """每个 ``FROM`` 都必须带 ``@sha256:``。

    这条把 A1 的前提变成 CI 约束：只要有人加一个浮动 tag 的阶段（或改回
    ``:latest``），测试就红。清单里"按 digest 固定=否"的提示是给人看的，
    这条断言是给流水线看的。
    """
    images = inv._base_images()
    assert images, "未从 Dockerfile 解析到任何 FROM 行（路径或格式变了？）"
    unpinned = [ref for ref, _, pinned in images if not pinned]
    assert unpinned == [], f"以下基础镜像未按 digest 固定：{unpinned}"


def test_app_version_matches_contract_baseline(rendered: str) -> None:
    """版本号必须与契约基线 V2.0.5 一致（A1 的"版本号与基线一致"）。

    三处必须同值：``Settings.app_version``、``pyproject.toml`` 的 version、
    Dockerfile 的 ``ARG APP_VERSION``。第三处尤其容易漏——不写它，镜像会继承
    基础镜像的 ``org.opencontainers.image.version``（实测为 uv 的
    ``0.9.30-python3.12-bookworm-slim``），制品库与事故复盘里会被认错版本。
    """
    assert Settings().app_version == "2.0.5"
    assert "`2.0.5`" in rendered
    assert inv.dockerfile_app_version() == Settings().app_version, (
        "Dockerfile 的 ARG APP_VERSION 与 Settings.app_version 不一致"
    )
    assert inv.package_version() == Settings().app_version, (
        "pyproject.toml 的 version 与 Settings.app_version 不一致"
    )


def test_render_is_deterministic(rendered: str) -> None:
    """两次渲染必须逐字节相同。

    清单里一旦混入时间戳或 commit，它就无法被"与代码比对"——测试只能退化成
    "文件存在"。确定性是可校验的前提。
    """
    assert inv.render() == rendered
