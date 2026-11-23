"""ComfyUI 网关适配器的失败映射与静默出错防线。

为什么这类适配器比 OpenAI 兼容更需要测
--------------------------------------
OpenAI 兼容是「一次请求 → 一个响应」，错了就是错误码。ComfyUI 是三段异步，
且**它会在一切正常的情况下产出错误的结果**：

- 注入点猜错 → 返回一张与提示词无关的图，状态 SUCCEEDED；
- 工作流有多个保存节点 → 交付了「其中一张」，另外几张无人知晓；
- ``/view`` 返回 HTML 错误页但 200 → 一个网页被当成产物存进资产台账。

这些都不是异常，是「成功」。所以下面的用例重点是钉住这些边界，而不是
「连通性」——真机连通性属于外部验收，不在这里（用 ``httpx.MockTransport``，
避免把网络抖动引进来让用例时而红时而绿）。
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from adapters.model_gateway.comfyui import ComfyUIGateway
from adapters.model_gateway.fake import deterministic_png
from adapters.model_gateway.ports import (
    GatewayMisconfiguredError,
    GatewayRejectedError,
    GatewayRetryableError,
    GenerationRequest,
)

_BASE = "http://comfy.example.com:8188"
_KEY = "comfy-proxy-token-do-not-log"
_PNG = deterministic_png("comfy-unit", size_bytes=256)

# ComfyUI「导出 (API)」格式：顶层是 节点 id → {class_type, inputs}。
_WORKFLOW = {
    "3": {"class_type": "KSampler", "inputs": {"seed": 1, "steps": 20}},
    "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "template positive"}},
    "7": {"class_type": "CLIPTextEncode", "inputs": {"text": "template negative"}},
    "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "ComfyUI"}},
}


def _request(**overrides) -> GenerationRequest:
    base = {
        "task_id": "gen-1",
        "prompt": "a lighthouse at dusk",
        "model": "",
        "media_type": "image/png",
    }
    base.update(overrides)
    return GenerationRequest(**base)


def _success_entry(
    prompt_id: str = "p-1",
    *,
    node: str = "9",
    filename: str = "ComfyUI_00001_.png",
    subfolder: str = "",
    kind: str = "output",
    images: list[dict] | None = None,
) -> dict:
    return {
        "prompt": [],
        "outputs": {
            node: {
                "images": images
                if images is not None
                else [{"filename": filename, "subfolder": subfolder, "type": kind}]
            }
        },
        "status": {"status_str": "success", "completed": True, "messages": []},
    }


def _error_entry(
    exception_type: str = "RuntimeError", message: str = "node blew up"
) -> dict:
    return {
        "outputs": {},
        "status": {
            "status_str": "error",
            "completed": False,
            "messages": [
                [
                    "execution_error",
                    {
                        "exception_type": exception_type,
                        "exception_message": message,
                    },
                ]
            ],
        },
    }


class _Comfy:
    """最小 ComfyUI 假服务：按路径分发，并记录收到的请求。"""

    def __init__(
        self,
        *,
        prompt_id: str = "p-1",
        entry: dict | None = None,
        pending_polls: int = 0,
        submit_status: int = 200,
        submit_body: dict | None = None,
        view_body: bytes = _PNG,
        view_headers: dict | None = None,
        view_status: int = 200,
        history_status: int = 200,
    ) -> None:
        self.prompt_id = prompt_id
        self.entry = entry if entry is not None else _success_entry(prompt_id)
        self.pending_polls = pending_polls
        self.submit_status = submit_status
        self.submit_body = submit_body
        self.view_body = view_body
        self.view_headers = view_headers or {"content-type": "image/png"}
        self.view_status = view_status
        self.history_status = history_status
        self.submitted: dict | None = None
        self.history_calls = 0
        self.view_params: dict | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/prompt":
            self.submitted = json.loads(request.content)
            if self.submit_body is not None:
                return httpx.Response(self.submit_status, json=self.submit_body)
            return httpx.Response(
                200, json={"prompt_id": self.prompt_id, "number": 0}
            )
        if path.startswith("/history/"):
            self.history_calls += 1
            if self.history_status >= 400:
                return httpx.Response(self.history_status, text="history unavailable")
            if self.history_calls <= self.pending_polls:
                return httpx.Response(200, json={})
            return httpx.Response(200, json={self.prompt_id: self.entry})
        if path == "/view":
            self.view_params = dict(request.url.params)
            return httpx.Response(
                self.view_status, content=self.view_body, headers=self.view_headers
            )
        return httpx.Response(404, text=f"unexpected path {path}")


def _gateway(tmp_path: Path, fake: _Comfy, *, workflow: dict | None = None, **kwargs):
    path = tmp_path / "workflow_api.json"
    path.write_text(json.dumps(workflow or _WORKFLOW), encoding="utf-8")
    client = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    return ComfyUIGateway(
        base_url=_BASE,
        workflow_path=path,
        prompt_node="6",
        client=client,
        poll_interval_seconds=0.01,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# 正常路径
# ---------------------------------------------------------------------------


async def test_generate_submits_injected_workflow_and_returns_bytes(tmp_path) -> None:
    fake = _Comfy()
    gateway = _gateway(tmp_path, fake, api_key=_KEY)

    artifact = await gateway.generate(_request())

    assert artifact.data == _PNG
    assert artifact.media_type == "image/png"
    assert artifact.upstream_request_id == "p-1"

    # 提交的是工作流图，且注入把模板里的字面量替换成了请求的提示词。
    assert fake.submitted is not None
    workflow = fake.submitted["prompt"]
    assert workflow["6"]["inputs"]["text"] == "a lighthouse at dusk"
    # 未注入的节点保持模板原值（没有被顺手改掉）。
    assert workflow["7"]["inputs"]["text"] == "template negative"
    assert workflow["3"]["inputs"]["seed"] == 1
    # 模板本体不被请求污染：第二次请求仍从原模板出发。
    assert _WORKFLOW["6"]["inputs"]["text"] == "template positive"

    # 产物按 filename/subfolder/type 取回，type 固定 output。
    assert fake.view_params == {
        "filename": "ComfyUI_00001_.png",
        "subfolder": "",
        "type": "output",
    }


async def test_polls_until_history_entry_appears(tmp_path) -> None:
    """任务在队列里时 /history 返回 {}，必须继续等而不是当成失败。"""
    fake = _Comfy(pending_polls=2)
    gateway = _gateway(tmp_path, fake)

    artifact = await gateway.generate(_request())

    assert artifact.data == _PNG
    assert fake.history_calls == 3


async def test_negative_prompt_and_node_inputs_are_applied(tmp_path) -> None:
    fake = _Comfy()
    gateway = _gateway(tmp_path, fake, negative_node="7")

    await gateway.generate(
        _request(
            extra={
                "negative_prompt": "blurry, lowres",
                "node_inputs": {"3": {"seed": 42, "steps": 30}},
            }
        )
    )

    assert fake.submitted is not None
    workflow = fake.submitted["prompt"]
    assert workflow["7"]["inputs"]["text"] == "blurry, lowres"
    assert workflow["3"]["inputs"]["seed"] == 42
    assert workflow["3"]["inputs"]["steps"] == 30


async def test_specified_output_node_selects_the_right_one(tmp_path) -> None:
    entry = _success_entry()
    entry["outputs"]["12"] = {
        "images": [{"filename": "preview.png", "subfolder": "", "type": "output"}]
    }
    fake = _Comfy(entry=entry)
    gateway = _gateway(tmp_path, fake, output_node="9")

    await gateway.generate(_request())

    assert fake.view_params is not None
    assert fake.view_params["filename"] == "ComfyUI_00001_.png"


# ---------------------------------------------------------------------------
# 确定性失败（重试不会变好）
# ---------------------------------------------------------------------------


async def test_submit_node_errors_is_rejected(tmp_path) -> None:
    fake = _Comfy(
        submit_status=400,
        submit_body={
            "error": {"type": "prompt_outputs_failed_validation"},
            "node_errors": {"6": {"errors": ["text is required"]}},
        },
    )
    gateway = _gateway(tmp_path, fake)

    with pytest.raises(GatewayRejectedError, match="拒绝"):
        await gateway.generate(_request())


async def test_submit_200_with_error_body_is_also_rejected(tmp_path) -> None:
    """有些代理会把 400 改写成 200：成功分支必须复核 body，不能只看状态码。"""
    fake = _Comfy(
        submit_status=200,
        submit_body={"error": {"message": "invalid prompt"}},
    )
    gateway = _gateway(tmp_path, fake)

    with pytest.raises(GatewayRejectedError, match="拒绝了工作流"):
        await gateway.generate(_request())


async def test_execution_error_is_rejected(tmp_path) -> None:
    fake = _Comfy(entry=_error_entry("RuntimeError", "unknown model file"))
    gateway = _gateway(tmp_path, fake)

    with pytest.raises(GatewayRejectedError, match="执行失败"):
        await gateway.generate(_request())


async def test_multiple_candidates_without_output_node_is_rejected(tmp_path) -> None:
    """多候选时静默取第一张，等于批量生成「只交付一张，其余去向无人知晓」。"""
    entry = _success_entry()
    entry["outputs"]["12"] = {
        "images": [{"filename": "second.png", "subfolder": "", "type": "output"}]
    }
    fake = _Comfy(entry=entry)
    gateway = _gateway(tmp_path, fake)

    with pytest.raises(GatewayRejectedError, match="产物候选"):
        await gateway.generate(_request())


async def test_batch_images_are_rejected_even_with_output_node(tmp_path) -> None:
    entry = _success_entry(
        images=[
            {"filename": "a.png", "subfolder": "", "type": "output"},
            {"filename": "b.png", "subfolder": "", "type": "output"},
        ]
    )
    fake = _Comfy(entry=entry)
    gateway = _gateway(tmp_path, fake, output_node="9")

    with pytest.raises(GatewayRejectedError, match="产物候选"):
        await gateway.generate(_request())


async def test_temp_type_is_rejected(tmp_path) -> None:
    fake = _Comfy(entry=_success_entry(kind="temp"))
    gateway = _gateway(tmp_path, fake)

    with pytest.raises(GatewayRejectedError, match="temp"):
        await gateway.generate(_request())


async def test_filename_with_traversal_is_rejected(tmp_path) -> None:
    fake = _Comfy(entry=_success_entry(filename="../etc/passwd"))
    gateway = _gateway(tmp_path, fake)

    with pytest.raises(GatewayRejectedError, match="形态异常"):
        await gateway.generate(_request())


async def test_view_returning_html_is_rejected(tmp_path) -> None:
    """200 + text/html 的错误页不能当产物：否则一个网页被存进资产台账。"""
    fake = _Comfy(
        view_body=b"<html>error</html>",
        view_headers={"content-type": "text/html; charset=utf-8"},
    )
    gateway = _gateway(tmp_path, fake)

    with pytest.raises(GatewayRejectedError, match="而非图像"):
        await gateway.generate(_request())


async def test_artifact_over_limit_is_rejected(tmp_path) -> None:
    fake = _Comfy()
    gateway = _gateway(tmp_path, fake, max_bytes=64)

    with pytest.raises(GatewayRejectedError, match="超出上限"):
        await gateway.generate(_request())


async def test_model_mismatch_is_rejected_not_ignored(tmp_path) -> None:
    """ComfyUI 下 model 是工作流标识；静默忽略会让调用方以为换了模型。"""
    fake = _Comfy()
    gateway = _gateway(tmp_path, fake, workflow_id="sd15")

    with pytest.raises(GatewayRejectedError, match="工作流标识"):
        await gateway.generate(_request(model="sdxl"))

    # 一致时正常放行。
    artifact = await gateway.generate(_request(model="sd15"))
    assert artifact.data == _PNG


async def test_node_inputs_unknown_node_is_rejected(tmp_path) -> None:
    fake = _Comfy()
    gateway = _gateway(tmp_path, fake)

    with pytest.raises(GatewayRejectedError, match="不存在的节点"):
        await gateway.generate(_request(extra={"node_inputs": {"99": {"seed": 1}}}))


async def test_node_inputs_unknown_input_is_rejected(tmp_path) -> None:
    """给节点加一个它没有的输入，ComfyUI 不会消费——那就是静默无效。"""
    fake = _Comfy()
    gateway = _gateway(tmp_path, fake)

    with pytest.raises(GatewayRejectedError, match="没有输入"):
        await gateway.generate(_request(extra={"node_inputs": {"3": {"nope": 1}}}))


# ---------------------------------------------------------------------------
# 瞬时失败（重试可能成功）
# ---------------------------------------------------------------------------


async def test_out_of_memory_is_retryable(tmp_path) -> None:
    """显存被别的任务占着是可自愈的；判成永久失败会把任务白丢。"""
    fake = _Comfy(entry=_error_entry("torch.OutOfMemoryError", "CUDA out of memory"))
    gateway = _gateway(tmp_path, fake)

    with pytest.raises(GatewayRetryableError, match="资源不足"):
        await gateway.generate(_request())


async def test_submit_5xx_is_retryable(tmp_path) -> None:
    fake = _Comfy(submit_status=503, submit_body={"error": "overloaded"})
    gateway = _gateway(tmp_path, fake)

    with pytest.raises(GatewayRetryableError):
        await gateway.generate(_request())


async def test_history_5xx_is_retryable(tmp_path) -> None:
    fake = _Comfy(history_status=502)
    gateway = _gateway(tmp_path, fake)

    with pytest.raises(GatewayRetryableError):
        await gateway.generate(_request())


async def test_job_timeout_is_retryable_and_says_why(tmp_path) -> None:
    """超时时任务很可能仍在队列里：必须说清「重试会重复排队」。"""
    fake = _Comfy(pending_polls=10**6)
    gateway = _gateway(tmp_path, fake, job_timeout_seconds=0.05)

    with pytest.raises(GatewayRetryableError, match="重复排队"):
        await gateway.generate(_request())


# ---------------------------------------------------------------------------
# 配置矛盾（构造期即拒绝）
# ---------------------------------------------------------------------------


def test_missing_workflow_file_is_misconfigured(tmp_path) -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(_Comfy().handler))
    with pytest.raises(GatewayMisconfiguredError, match="读取失败"):
        ComfyUIGateway(
            base_url=_BASE,
            workflow_path=tmp_path / "absent.json",
            prompt_node="6",
            client=client,
        )


def test_ui_export_format_is_rejected_with_actionable_hint(tmp_path) -> None:
    """UI 导出的 workflow 是 {nodes, links}；提交后只会得到一个难懂的校验错。"""
    path = tmp_path / "ui_workflow.json"
    path.write_text(json.dumps({"nodes": [], "links": []}), encoding="utf-8")
    client = httpx.AsyncClient(transport=httpx.MockTransport(_Comfy().handler))

    with pytest.raises(GatewayMisconfiguredError, match="导出"):
        ComfyUIGateway(
            base_url=_BASE, workflow_path=path, prompt_node="6", client=client
        )


def test_missing_prompt_node_is_misconfigured(tmp_path) -> None:
    path = tmp_path / "workflow_api.json"
    path.write_text(json.dumps(_WORKFLOW), encoding="utf-8")
    client = httpx.AsyncClient(transport=httpx.MockTransport(_Comfy().handler))

    with pytest.raises(GatewayMisconfiguredError, match="不存在于工作流模板"):
        ComfyUIGateway(
            base_url=_BASE, workflow_path=path, prompt_node="42", client=client
        )


def test_prompt_node_wired_to_upstream_is_misconfigured(tmp_path) -> None:
    """注入点接了上游连线时，覆盖会静默切断连线并改变图语义。"""
    workflow = json.loads(json.dumps(_WORKFLOW))
    workflow["6"]["inputs"]["text"] = ["4", 0]
    path = tmp_path / "workflow_api.json"
    path.write_text(json.dumps(workflow), encoding="utf-8")
    client = httpx.AsyncClient(transport=httpx.MockTransport(_Comfy().handler))

    with pytest.raises(GatewayMisconfiguredError, match="切断连线"):
        ComfyUIGateway(
            base_url=_BASE, workflow_path=path, prompt_node="6", client=client
        )


def test_empty_prompt_node_is_misconfigured(tmp_path) -> None:
    path = tmp_path / "workflow_api.json"
    path.write_text(json.dumps(_WORKFLOW), encoding="utf-8")
    client = httpx.AsyncClient(transport=httpx.MockTransport(_Comfy().handler))

    with pytest.raises(GatewayMisconfiguredError, match="PROMPT_NODE"):
        ComfyUIGateway(
            base_url=_BASE, workflow_path=path, prompt_node="", client=client
        )


def test_api_key_not_in_repr(tmp_path) -> None:
    """repr 会进异常、日志、调试器快照。"""
    fake = _Comfy()
    gateway = _gateway(tmp_path, fake, api_key=_KEY)

    assert _KEY not in repr(gateway)
