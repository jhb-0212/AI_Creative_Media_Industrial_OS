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
_COMFY_TESTS = "tests/unit/test_comfyui_gateway.py"
_EXECUTOR_TESTS = "tests/unit/test_generation_executor_wiring.py"
_CONFIG_TESTS = "tests/unit/test_generation_executor_config.py"

_CASES = [
    BadState(
        label="去掉扩展名白名单",
        path="adapters/storage/local_fs.py",
        old="        if ext not in _ALLOWED_EXTENSIONS:",
        new="        if False:  # BAD-STATE",
        expected_test="test_locator_must_be_hex_not_arbitrary_text",
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
    # 以下三条守的是 ComfyUI 特有的「静默出错」：不会抛异常，只会交付错的东西。
    # 它们比错误码更值得固化，因为没有任何日志信号提示你出了事。
    BadState(
        label="ComfyUI 注入点接了连线也照塞",
        path="adapters/model_gateway/comfyui.py",
        old="    if not isinstance(current, str):\n        raise GatewayMisconfiguredError(",
        new="    if False:  # BAD-STATE\n        raise GatewayMisconfiguredError(",
        expected_test="test_prompt_node_wired_to_upstream_is_misconfigured",
    ),
    BadState(
        label="ComfyUI 多候选时静默取第一张",
        path="adapters/model_gateway/comfyui.py",
        old="        if len(candidates) != 1:",
        new="        if len(candidates) == 0:  # BAD-STATE",
        expected_test="test_multiple_candidates_without_output_node_is_rejected",
    ),
    BadState(
        label="ComfyUI 把 HTML 错误页当产物",
        path="adapters/model_gateway/comfyui.py",
        # 去掉媒体类型硬判定：错误页会按 image/png 存进资产台账。
        old='                if header and not header.startswith(("image/", "video/")):',
        new="                if False:  # BAD-STATE",
        expected_test="test_view_returning_html_is_rejected",
    ),
]

_TEST_FILE_BY_LABEL = {
    "去掉扩展名白名单": _STORAGE_TESTS,
    "读取时不判符号链接": _STORAGE_TESTS,
    "5xx/429 归成拒绝（不再可重试）": _GATEWAY_TESTS,
    "只信 content-length，不做累计计数": _GATEWAY_TESTS,
    "不校验产物 URL 协议": _GATEWAY_TESTS,
    "prompt_ref 直接当正文": _EXECUTOR_TESTS,
    "可重试失败被归成拒绝": _EXECUTOR_TESTS,
    "缺存储根目录不报错": _CONFIG_TESTS,
    "ComfyUI 注入点接了连线也照塞": _COMFY_TESTS,
    "ComfyUI 多候选时静默取第一张": _COMFY_TESTS,
    "ComfyUI 把 HTML 错误页当产物": _COMFY_TESTS,
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
