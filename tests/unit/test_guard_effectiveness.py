"""守卫有效性自检：证明「把防线拆掉，对应用例真的会红」。

为什么需要这个装置
------------------
用例全绿只证明「当前实现能过」，不证明「用例守住了什么」。一条写错的断言
（或一个永远为真的条件）会照样全绿，等到实现退化时也不报警——那是最坏的
一种闸门：它给人安全感，却不产生任何保护。

本装置的做法是**注入坏状态 → 跑 → 恢复**，并要求结果满足两条：

1. ``exit != 0``；
2. 输出里出现 ``FAILED`` 且**没有** ``ERROR``。

第 2 条是必须的。之前用 ``--basetemp=/tmp/x`` 在 Windows 上跑，pytest 把
``/tmp/x`` 解析成 ``C:\\tmp\\x``（不存在）→ 每个用例都是 ``ERROR``（夹具失败），
``exit`` 同样是 1。若只看退出码，就会把「环境坏了」当成「闸门有效」——
这种自证正是本装置要排除的失败模式，因此 basetemp 一律用 ``mkdtemp()``
生成的绝对路径。
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

import pytest


@dataclass(frozen=True)
class BadState:
    """一处坏状态：文件、要替换的片段、替换成什么、期望被抓住的用例。"""

    label: str
    path: str
    old: str
    new: str
    expected_test: str


@contextmanager
def _patched(state: BadState) -> Iterator[None]:
    target = pathlib.Path(state.path)
    original = target.read_text(encoding="utf-8")
    if state.old not in original:
        raise AssertionError(
            f"[{state.label}] 找不到要替换的片段，坏状态无法注入：{state.old[:60]!r}"
        )
    try:
        target.write_text(original.replace(state.old, state.new), encoding="utf-8")
        yield
    finally:
        target.write_text(original, encoding="utf-8")


def _run(test_file: str) -> subprocess.CompletedProcess[str]:
    basetemp = tempfile.mkdtemp(prefix="badstate-")
    try:
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                test_file,
                "-q",
                "--no-header",
                f"--basetemp={basetemp}",
                "-p",
                "no:cacheprovider",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    finally:
        shutil.rmtree(basetemp, ignore_errors=True)


_STORAGE_TESTS = "tests/unit/test_local_storage_guard.py"
_GATEWAY_TESTS = "tests/unit/test_openai_compatible_gateway.py"
_EXECUTOR_TESTS = "tests/unit/test_generation_executor_wiring.py"
_CONFIG_TESTS = "tests/unit/test_generation_executor_config.py"
_LOCATOR_TESTS = "tests/unit/test_storage_locator.py"
_ASSET_DOMAIN_TESTS = "tests/unit/test_asset_domain.py"

_CASES = [
    # 说明：原先这里有一条「去掉扩展名白名单」的坏状态，注入点是 local_fs 的
    # `_ALLOWED_EXTENSIONS`。定位符语法收敛到 packages/shared/storage_locator
    # 之后，那条注入点已不存在；更重要的是**即使存在也注入无效**——白名单在
    # `parse_locator` 的分段判定与 `_STRICT_RE`（契约 pattern 的扩展名枚举）
    # 里各判一次，单点放宽会被另一条拦住。
    #
    # 这是有意的纵深防御，但它破坏了「单点变异能否证伪」这个前提。处理方式不是
    # 削弱防线，而是**只登记能单点证伪的条目**，并如实说明哪些防线做不到：
    # 对它们而言「注入后闸门没红」既可能是闸门失效，也可能是另一条防线接手，
    # 二者不可区分 —— 不可区分的证据不如不要。
    BadState(
        label="build_locator 不校验摘要形状",
        path="packages/shared/storage_locator.py",
        old='    if not re.fullmatch(r"[0-9a-f]{64}", digest):',
        new="    if False:  # BAD-STATE",
        expected_test="test_build_rejects_bad_digest_and_extension",
    ),
    BadState(
        label="领域层不判定位符与 sha256 一致",
        path="services/asset_service/domain/asset_version.py",
        old="        if parsed.sha256 != self.sha256:",
        new="        if False:  # BAD-STATE",
        expected_test="test_storage_ref_must_embed_the_declared_sha256",
    ),
    BadState(
        label="读取时不判符号链接",
        path="adapters/storage/local_fs.py",
        old="        if not os.path.isfile(target) or os.path.islink(target):",
        new="        if not os.path.isfile(target):  # BAD-STATE",
        expected_test="test_symlink_in_root_is_refused",
    ),
    BadState(
        label="5xx/429 归成拒绝（不再可重试）",
        path="adapters/model_gateway/openai_compatible.py",
        old=(
            "        if response.status_code == 429 or response.status_code >= 500:\n"
            "            raise GatewayRetryableError("
        ),
        new=(
            "        if response.status_code == 429 or response.status_code >= 500:\n"
            "            raise GatewayRejectedError("
        ),
        expected_test="test_5xx_and_429_are_retryable",
    ),
    BadState(
        label="只信 content-length，不做累计计数",
        # 这段逻辑已抽到 bounds.py（两个协议共用一份边界实现）。变异点必须
        # 跟着**被测事实**走而不是跟着文件走：留在旧路径只会让装置报「找不到
        # 片段」，那证明的是装置失配，不是闸门无效——两者必须能区分。
        path="adapters/model_gateway/bounds.py",
        old="        if total > limit:",
        new="        if False:  # BAD-STATE",
        expected_test="test_lying_content_length_is_caught_by_cumulative_count",
    ),
    BadState(
        label="不校验产物 URL 协议",
        path="adapters/model_gateway/openai_compatible.py",
        old='            if urlparse(current).scheme not in {"http", "https"}:',
        new="            if False:  # BAD-STATE",
        expected_test="test_non_http_artifact_url_is_rejected",
    ),
    BadState(
        label="prompt_ref 直接当正文",
        path="workers/generation/executor.py",
        old='            prompt = await self._resolve_prompt(getattr(task, "prompt_ref", ""))',
        new='            prompt = getattr(task, "prompt_ref", "")  # BAD-STATE',
        expected_test="test_unresolvable_prompt_never_calls_gateway",
    ),
    BadState(
        label="可重试失败被归成拒绝",
        path="workers/generation/executor.py",
        old='            return _failed("gateway-retryable", str(exc))',
        new='            return _failed("gateway-rejected", str(exc))  # BAD-STATE',
        expected_test="test_gateway_failures_are_classified[timeout-gateway-retryable]",
    ),
    BadState(
        label="缺存储根目录不报错",
        path="workers/generation/__main__.py",
        old="    if not storage_root:\n        raise ValueError(",
        new="    if False:  # BAD-STATE\n        raise ValueError(",
        expected_test="test_missing_storage_root_is_rejected",
    ),
]

_TEST_FILE_BY_LABEL = {
    "build_locator 不校验摘要形状": _LOCATOR_TESTS,
    "领域层不判定位符与 sha256 一致": _ASSET_DOMAIN_TESTS,
    "读取时不判符号链接": _STORAGE_TESTS,
    "5xx/429 归成拒绝（不再可重试）": _GATEWAY_TESTS,
    "只信 content-length，不做累计计数": _GATEWAY_TESTS,
    "不校验产物 URL 协议": _GATEWAY_TESTS,
    "prompt_ref 直接当正文": _EXECUTOR_TESTS,
    "可重试失败被归成拒绝": _EXECUTOR_TESTS,
    "缺存储根目录不报错": _CONFIG_TESTS,
}


@pytest.mark.parametrize("state", _CASES, ids=lambda s: s.label)
def test_guard_catches_bad_state(state: BadState) -> None:
    """注入坏状态后，对应用例必须以 FAILED 出现（而不是 ERROR）。"""
    with _patched(state):
        result = _run(_TEST_FILE_BY_LABEL[state.label])

    output = result.stdout + result.stderr
    assert "ERROR" not in output, (
        f"[{state.label}] 出现 ERROR 说明跑的是环境问题而非断言失败，"
        f"这次验证无效：\n{output[-800:]}"
    )
    assert result.returncode != 0, f"[{state.label}] 坏状态下竟然全绿：\n{output[-800:]}"
    assert state.expected_test in output, (
        f"[{state.label}] 期望 {state.expected_test} 报红，实际失败的是别的用例：\n"
        f"{output[-800:]}"
    )
