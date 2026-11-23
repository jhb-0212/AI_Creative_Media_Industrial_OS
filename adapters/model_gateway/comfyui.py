"""ComfyUI 网关实现（``POST /prompt`` 提交 → 轮询 ``/history`` → ``/view`` 取产物）。

为什么它不能像 OpenAI 兼容那样「一次调用拿到字节」
--------------------------------------------------
ComfyUI 是**工作流执行器**，不是提示词接口。它的调用形状是三段异步：

    POST /prompt             → {"prompt_id": "...", "number": 0}
    GET  /history/{id}       → 反复轮询，直到 status 终结
    GET  /view?filename=...  → 取回单个图像字节

这带来三个「不做就会静默出错」的设计点：

1. **必须有工作流模板**。ComfyUI 只接受 ``{node_id: {class_type, inputs}}`` 形式
   的图，没有任何「给我一句话」的入口。因此模板由部署提供，请求只提供参数。
   **不允许调用方在请求里带工作流**——那等于让调用方执行任意图，而模板就不再
   是「这个部署用哪个工作流」的单一事实源。
2. **注入点必须显式配置，不能猜**。「找第一个 CLIPTextEncode 塞进去」在多
   CLIPTextEncode 的图里必然猜错。猜错的后果不是报错，而是：返回一张内容完全
   无关的图、全程无错、状态 SUCCEEDED。**这是不产生信号的失败**，比报错严重。
3. **超时归可重试，但代价要写清**。轮询超过总时限时任务很可能仍排在 ComfyUI
   队列里，重试会重复排队、重复占用 GPU。归 ``GatewayRetryableError`` 是因为
   「任务白丢」比「多跑一次」更糟，而重复成本由预算域（P1-05 的预留/结算）
   约束——这个权衡留在代码里，不留给读代码的人去猜。

失败映射（对应 ``ports.py`` 的二分）
------------------------------------
| 情形 | 抛出 |
|---|---|
| 连接失败 / 超时 / 5xx / 429 | ``GatewayRetryableError`` |
| ``POST /prompt`` 返回 4xx 或 ``error`` / ``node_errors`` | ``GatewayRejectedError`` |
| 执行失败为 OOM / CUDA / 磁盘满（资源瞬时不足） | ``GatewayRetryableError`` |
| 执行失败的其他异常（工作流或参数错误） | ``GatewayRejectedError`` |
| 轮询超过 ``job_timeout`` | ``GatewayRetryableError`` |
| ``outputs`` 无产物 / 多候选 / 类型不符 | ``GatewayRejectedError`` |
| 模板不存在、非法、注入点缺失或接了连线 | ``GatewayMisconfiguredError``（构造期） |

为什么「注入点接了连线」要在构造期拒绝
--------------------------------------
模板里 ``inputs.text`` 的合法值有两种：字面量字符串，或指向上游节点的连线
``["6", 0]``。注入会**覆盖**这个值：若它本来是连线，覆盖就等于静默切断连线、
把「取上游节点输出的文本」换成「用这句话」，图的语义被改了而没有任何提示。
所以构造期就要求注入点是字面量，让配置错误在启动时暴露，而不是在第一张
错的图上暴露。
"""

from __future__ import annotations

import asyncio
import copy
import json
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from adapters.model_gateway.bounds import (
    declared_media_type,
    excerpt,
    read_capped,
)
from adapters.model_gateway.ports import (
    GatewayArtifact,
    GatewayMisconfiguredError,
    GatewayRejectedError,
    GatewayRetryableError,
    GenerationRequest,
)

# 资源瞬时不足的判据。刻意只收这几种：把「任何执行失败」都算可重试会让
# 一个坏工作流无限重跑烧 GPU；把它们算永久失败又会让「显存被别的任务占着」
# 这种真实的自愈场景变成任务丢失。
_RESOURCE_EXHAUSTION_MARKERS = (
    "out of memory",
    "cuda error",
    "no space left on device",
)


class ComfyUIGateway:
    """把 ``GenerationRequest`` 翻译成「提交工作流 → 等结果 → 取图」。"""

    def __init__(
        self,
        *,
        base_url: str,
        workflow_path: str | Path,
        prompt_node: str,
        prompt_field: str = "text",
        negative_node: str = "",
        negative_field: str = "text",
        output_node: str = "",
        workflow_id: str = "",
        api_key: str = "",
        timeout_seconds: float = 120.0,
        job_timeout_seconds: float = 900.0,
        poll_interval_seconds: float = 2.0,
        max_bytes: int = 256 * 1024 * 1024,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        scheme = urlparse(base_url).scheme if base_url else ""
        if scheme not in {"http", "https"}:
            raise GatewayMisconfiguredError(
                f"MODEL_GATEWAY_BASE_URL 必须是 http(s) 绝对地址，当前={base_url!r}"
            )
        if not prompt_node:
            raise GatewayMisconfiguredError(
                "MODEL_GATEWAY_COMFYUI_PROMPT_NODE 为空："
                "不指定注入节点就只能靠猜，而猜错会产出与意图无关的图且全程无错"
            )
        if max_bytes <= 0:
            raise ValueError("max_bytes 必须是正数：无上限的产物下载会打爆磁盘")
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds 必须为正：零间隔轮询等于打满 ComfyUI")
        if job_timeout_seconds <= 0:
            raise ValueError("job_timeout_seconds 必须为正：无总时限的轮询会永远挂住 worker")

        template = _load_workflow(Path(workflow_path))
        _require_literal_input(
            template, prompt_node, prompt_field, role="提示词"
        )
        if negative_node:
            _require_literal_input(
                template, negative_node, negative_field, role="负向提示词"
            )
        if output_node and output_node not in template:
            raise GatewayMisconfiguredError(
                f"MODEL_GATEWAY_COMFYUI_OUTPUT_NODE={output_node!r} 不存在于工作流模板"
            )

        self._base_url = base_url.rstrip("/")
        self._template = template
        self._prompt_node = prompt_node
        self._prompt_field = prompt_field
        self._negative_node = negative_node
        self._negative_field = negative_field
        self._output_node = output_node
        self._workflow_id = workflow_id
        self._api_key = api_key
        self._max_bytes = max_bytes
        self._job_timeout = job_timeout_seconds
        self._poll_interval = poll_interval_seconds
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds, connect=10.0),
            follow_redirects=False,
        )
        self._owns_client = client is None

    def __repr__(self) -> str:
        # 刻意不含 api_key：repr 会进异常、日志、调试器快照。
        return (
            f"ComfyUIGateway(base_url={self._base_url!r}, "
            f"workflow_id={self._workflow_id!r}, prompt_node={self._prompt_node!r}, "
            f"max_bytes={self._max_bytes})"
        )

    async def aclose(self) -> None:
        """只关闭自己创建的 client：外部注入的由注入方管理生命周期。"""
        if self._owns_client:
            await self._client.aclose()

    async def generate(self, request: GenerationRequest) -> GatewayArtifact:
        self._check_model(request)
        workflow = self._build_workflow(request)
        prompt_id = await self._submit(workflow, request)
        entry = await self._wait_for_completion(prompt_id, request)
        self._raise_if_failed(entry, prompt_id, request)
        reference = self._select_artifact(entry, prompt_id)
        data, media_type = await self._download(reference, request)
        return GatewayArtifact(
            data=data,
            media_type=media_type,
            declared_sha256="",  # 该协议不返回摘要，由存储层自行计算
            upstream_request_id=prompt_id,
        )

    # --- 请求构造 ---------------------------------------------------------

    def _check_model(self, request: GenerationRequest) -> None:
        """``model`` 在这条协议里是「工作流标识」，不做静默忽略。

        OpenAI 兼容下 ``model`` 决定用哪个模型；ComfyUI 下模型由工作流里的
        CheckpointLoader 决定，换字符串不会换模板。静默忽略会让调用方以为
        「换了模型」，实际跑的仍是同一张图——所以不匹配就明确拒绝。
        """
        if not request.model:
            return
        if request.model == self._workflow_id:
            return
        raise GatewayRejectedError(
            f"请求指定 model={request.model!r}，但本部署登记的工作流标识是 "
            f"{self._workflow_id!r}。ComfyUI 的模型由工作流决定，换 model 不会换模板；"
            "静默忽略会让你以为换了模型。请改工作流模板或改请求里的 model。"
        )

    def _build_workflow(self, request: GenerationRequest) -> dict[str, Any]:
        workflow: dict[str, Any] = copy.deepcopy(self._template)

        inputs = _literal_inputs(workflow, self._prompt_node)
        inputs[self._prompt_field] = request.prompt

        negative = request.extra.get("negative_prompt")
        if self._negative_node and isinstance(negative, str) and negative:
            neg_inputs = _literal_inputs(workflow, self._negative_node)
            neg_inputs[self._negative_field] = negative

        self._apply_node_inputs(workflow, request.extra.get("node_inputs"))
        return workflow

    @staticmethod
    def _apply_node_inputs(workflow: dict[str, Any], patch: object) -> None:
        """按 ``extra.node_inputs`` 覆盖节点输入（如 seed / width / height）。

        这是「不猜」的通用出口：调用方要改哪个节点的哪个输入，就明确说出来。
        为每个参数加一个配置项（SEED_NODE / WIDTH_NODE / …）会让配置面随工作流
        线性膨胀，而工作流的节点 id 本来就是我们无法预知的东西。
        """
        if patch is None:
            return
        if not isinstance(patch, Mapping):
            raise GatewayRejectedError(
                f"extra.node_inputs 必须是「节点 id → 输入覆盖」对象，当前是 "
                f"{type(patch).__name__}"
            )
        for node_id, values in patch.items():
            if not isinstance(values, Mapping):
                raise GatewayRejectedError(
                    f"extra.node_inputs[{node_id!r}] 必须是对象"
                )
            node = workflow.get(str(node_id))
            if not isinstance(node, dict) or not isinstance(node.get("inputs"), dict):
                raise GatewayRejectedError(
                    f"extra.node_inputs 指定了不存在的节点 {node_id!r}"
                )
            target: dict[str, Any] = node["inputs"]
            for key, value in values.items():
                if key == "class_type":
                    raise GatewayRejectedError(
                        "extra.node_inputs 不得改写 class_type：模板是唯一事实源"
                    )
                if key not in target:
                    raise GatewayRejectedError(
                        f"节点 {node_id!r} 没有输入 {key!r}"
                        f"（现有输入: {sorted(target)}）：新增未知输入不会被消费"
                    )
                target[key] = value

    # --- 三段调用 ---------------------------------------------------------

    async def _submit(
        self, workflow: dict[str, Any], request: GenerationRequest
    ) -> str:
        response = await self._post_json("/prompt", {"prompt": workflow}, request)
        body = _parse_object(response, request)
        if not body.get("prompt_id") or body.get("error") or body.get("node_errors"):
            # ComfyUI 校验失败时是 HTTP 400 + error/node_errors；但个别代理会把
            # 状态码改写成 200，所以成功分支也必须复核 body，不能只看状态码。
            detail = json.dumps(
                {
                    "error": body.get("error"),
                    "node_errors": body.get("node_errors"),
                },
                ensure_ascii=False,
                default=str,
            )
            raise GatewayRejectedError(
                f"ComfyUI 拒绝了工作流（task={request.task_id}）: {detail[:200]}"
            )
        prompt_id = body.get("prompt_id")
        if not isinstance(prompt_id, str):
            raise GatewayRejectedError(
                f"ComfyUI 返回的 prompt_id 不是字符串: {type(prompt_id).__name__}"
            )
        return prompt_id

    async def _wait_for_completion(
        self, prompt_id: str, request: GenerationRequest
    ) -> dict[str, Any]:
        deadline = time.monotonic() + self._job_timeout
        while True:
            entry = await self._fetch_history(prompt_id, request)
            status = entry.get("status")
            if isinstance(status, dict) and (
                status.get("completed") is True
                or status.get("status_str") in {"success", "error"}
            ):
                return entry
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise GatewayRetryableError(
                    f"工作流 {prompt_id} 在 {self._job_timeout:.0f}s 内未完成"
                    f"（task={request.task_id}）：任务可能仍在 ComfyUI 队列中，"
                    "重试会重复排队并重复占用 GPU——重复成本应由预算域约束"
                )
            await asyncio.sleep(min(self._poll_interval, remaining))

    async def _fetch_history(
        self, prompt_id: str, request: GenerationRequest
    ) -> dict[str, Any]:
        try:
            response = await self._client.get(
                f"{self._base_url}/history/{prompt_id}", headers=self._headers()
            )
        except httpx.TimeoutException as exc:
            raise GatewayRetryableError(
                f"查询工作流状态超时（task={request.task_id}, prompt={prompt_id}）"
            ) from exc
        except httpx.TransportError as exc:
            raise GatewayRetryableError(
                f"查询工作流状态连接失败（task={request.task_id}）: {type(exc).__name__}"
            ) from exc

        if response.status_code == 429 or response.status_code >= 500:
            raise GatewayRetryableError(
                f"查询工作流状态返回 {response.status_code}"
                f"（task={request.task_id}）: {excerpt(response)}"
            )
        if response.status_code >= 400:
            raise GatewayRejectedError(
                f"查询工作流状态被拒绝 {response.status_code}"
                f"（task={request.task_id}）: {excerpt(response)}"
            )

        body = _parse_object(response, request)
        entry = body.get(prompt_id)
        if entry is None:
            # 尚未进入历史：任务还在队列里，继续等。
            return {}
        if not isinstance(entry, dict):
            raise GatewayRejectedError(
                f"/history 中 {prompt_id} 的值不是对象: {type(entry).__name__}"
            )
        return entry

    def _raise_if_failed(
        self, entry: dict[str, Any], prompt_id: str, request: GenerationRequest
    ) -> None:
        status = entry.get("status")
        if not isinstance(status, dict):
            raise GatewayRejectedError(
                f"工作流 {prompt_id} 的历史记录缺少 status 对象"
            )
        if status.get("status_str") != "error":
            return

        detail = _execution_error_text(status.get("messages"))
        if _is_resource_exhaustion(detail):
            # 显存/磁盘的瞬时状态：换个时机可能就够。重复成本由预算域约束。
            raise GatewayRetryableError(
                f"工作流 {prompt_id} 因资源不足失败（task={request.task_id}）: "
                f"{detail[:200]}"
            )
        raise GatewayRejectedError(
            f"工作流 {prompt_id} 执行失败（task={request.task_id}）: "
            f"{detail[:200] or '未给出 execution_error'}"
        )

    def _select_artifact(self, entry: dict[str, Any], prompt_id: str) -> dict[str, Any]:
        outputs = entry.get("outputs")
        if not isinstance(outputs, dict) or not outputs:
            raise GatewayRejectedError(
                f"工作流 {prompt_id} 已结束但没有任何 outputs："
                "模板里可能缺少保存节点（SaveImage 之类）"
            )

        candidates: list[tuple[str, dict[str, Any]]] = []
        for node_id, ui in outputs.items():
            if not isinstance(ui, dict):
                continue
            images = ui.get("images")
            if not isinstance(images, list):
                continue
            for image in images:
                if isinstance(image, dict):
                    candidates.append((str(node_id), image))

        if self._output_node:
            candidates = [c for c in candidates if c[0] == self._output_node]
            if not candidates:
                raise GatewayRejectedError(
                    f"指定的产物节点 {self._output_node!r} 没有产出图像"
                )

        if len(candidates) != 1:
            nodes = sorted({node for node, _ in candidates})
            raise GatewayRejectedError(
                f"产物候选 {len(candidates)} 个（节点 {nodes}），无法确定哪一个属于"
                f"本任务（prompt={prompt_id}）。请配置 "
                "MODEL_GATEWAY_COMFYUI_OUTPUT_NODE 指定节点，或让工作流只有一个"
                "保存节点且 batch_size=1。静默取第一张会让批量生成变成"
                "「只交付一张，其余去向无人知晓」。"
            )
        return candidates[0][1]

    async def _download(
        self, reference: dict[str, Any], request: GenerationRequest
    ) -> tuple[bytes, str]:
        filename = _require_component(reference.get("filename"), field="filename")
        subfolder = _require_component(
            reference.get("subfolder", ""), field="subfolder", allow_empty=True
        )
        kind = reference.get("type", "output")
        if kind != "output":
            raise GatewayRejectedError(
                f"产物类型 {kind!r} 不是 'output'：temp 目录会被其他任务覆盖或清理，"
                "它不是「本次产物」的可靠存放位置"
            )

        params = {"filename": filename, "subfolder": subfolder, "type": "output"}
        try:
            async with self._client.stream(
                "GET", f"{self._base_url}/view", params=params, headers=self._headers()
            ) as response:
                if response.status_code == 429 or response.status_code >= 500:
                    raise GatewayRetryableError(
                        f"/view 返回 {response.status_code}"
                        f"（task={request.task_id}）: {excerpt(response)}"
                    )
                if response.status_code >= 400:
                    raise GatewayRejectedError(
                        f"取回产物失败 {response.status_code}"
                        f"（task={request.task_id}）: {excerpt(response)}"
                    )
                # ComfyUI 出错时可能回 200 + text/html 错误页。照单收下会把一个
                # 网页存成「产物」，所以这里必须硬判定，不能像通用下载那样回落。
                header = declared_media_type(response)
                if header and not header.startswith(("image/", "video/")):
                    raise GatewayRejectedError(
                        f"/view 返回 {header} 而非图像（task={request.task_id}）："
                        "产物取回失败，不是瞬时问题"
                    )
                data = await read_capped(
                    response, limit=self._max_bytes, task_id=request.task_id
                )
                return data, header or request.media_type
        except httpx.TimeoutException as exc:
            raise GatewayRetryableError(
                f"取回产物超时（task={request.task_id}）"
            ) from exc
        except httpx.TransportError as exc:
            raise GatewayRetryableError(
                f"取回产物连接失败（task={request.task_id}）: {type(exc).__name__}"
            ) from exc

    # --- 内部 -------------------------------------------------------------

    async def _post_json(
        self, path: str, payload: dict[str, Any], request: GenerationRequest
    ) -> httpx.Response:
        try:
            response = await self._client.post(
                f"{self._base_url}{path}", json=payload, headers=self._headers()
            )
        except httpx.TimeoutException as exc:
            raise GatewayRetryableError(
                f"提交工作流超时（task={request.task_id}）"
            ) from exc
        except httpx.TransportError as exc:
            raise GatewayRetryableError(
                f"提交工作流连接失败（task={request.task_id}）: {type(exc).__name__}"
            ) from exc

        if response.status_code == 429 or response.status_code >= 500:
            raise GatewayRetryableError(
                f"ComfyUI 返回 {response.status_code}"
                f"（task={request.task_id}）: {excerpt(response)}"
            )
        if response.status_code >= 400:
            raise GatewayRejectedError(
                f"ComfyUI 拒绝请求 {response.status_code}"
                f"（task={request.task_id}）: {excerpt(response)}"
            )
        return response

    def _headers(self) -> dict[str, str]:
        # ComfyUI 本身无鉴权；部署在反向代理后时由代理要求 token，故可选。
        return {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}


# --- 模块级辅助 -----------------------------------------------------------


def _load_workflow(path: Path) -> dict[str, Any]:
    """读取并校验工作流模板（ComfyUI 的「导出 (API)」格式）。"""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise GatewayMisconfiguredError(f"工作流模板读取失败 {path}: {exc}") from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise GatewayMisconfiguredError(f"工作流模板不是合法 JSON {path}: {exc}") from exc
    if not isinstance(data, dict) or not data:
        raise GatewayMisconfiguredError(
            f"工作流模板必须是「节点 id → 节点」的非空对象：{path}"
        )
    for node_id, node in data.items():
        # UI 导出的 workflow 是 {"nodes": [...], "links": [...]}，直接提交会被
        # ComfyUI 回一个难懂的校验错。这里提前判，并给出可执行的提示。
        if not isinstance(node, dict) or "class_type" not in node or "inputs" not in node:
            raise GatewayMisconfiguredError(
                f"工作流模板 {path} 的节点 {node_id!r} 缺少 class_type/inputs："
                "请在 ComfyUI 里用「导出 (API)」而不是「导出」"
            )
    return data


def _require_literal_input(
    template: dict[str, Any], node_id: str, field: str, *, role: str
) -> None:
    """要求注入点在模板里存在，且当前值是字面量字符串（不是上游连线）。"""
    node = template.get(node_id)
    if not isinstance(node, dict):
        raise GatewayMisconfiguredError(
            f"{role}注入节点 {node_id!r} 不存在于工作流模板"
        )
    inputs = node.get("inputs")
    if not isinstance(inputs, dict):
        raise GatewayMisconfiguredError(f"{role}注入节点 {node_id!r} 没有 inputs")
    if field not in inputs:
        raise GatewayMisconfiguredError(
            f"{role}注入节点 {node_id!r} 没有输入 {field!r}"
            f"（现有输入: {sorted(inputs)}）"
        )
    current = inputs[field]
    if not isinstance(current, str):
        raise GatewayMisconfiguredError(
            f"{role}注入节点 {node_id!r} 的 {field!r} 当前值是 "
            f"{type(current).__name__} 而非字符串：该输入很可能接了上游连线"
            "（形如 [\"6\", 0]），覆盖它会静默切断连线并改变图的语义。"
            "请在模板里把该输入设为字面量，或改用另一个文本节点。"
        )


def _literal_inputs(workflow: dict[str, Any], node_id: str) -> dict[str, Any]:
    """取节点 inputs（构造期已校验，这里只做类型收窄，便于静态检查）。"""
    node = workflow[node_id]
    inputs = node["inputs"]
    if not isinstance(inputs, dict):  # pragma: no cover - 构造期已保证
        raise GatewayMisconfiguredError(f"节点 {node_id!r} 的 inputs 不是对象")
    return inputs


def _parse_object(response: httpx.Response, request: GenerationRequest) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError as exc:
        raise GatewayRejectedError(
            f"ComfyUI 响应不是 JSON（task={request.task_id}, "
            f"status={response.status_code}）: {excerpt(response)}"
        ) from exc
    if not isinstance(body, dict):
        raise GatewayRejectedError(
            f"ComfyUI 响应顶层不是对象: {type(body).__name__}"
        )
    return body


def _execution_error_text(messages: object) -> str:
    """从 ``status.messages`` 里取第一条 ``execution_error`` 的描述。

    ComfyUI 里 messages 是 ``[(event, data), ...]``；JSON 化后成为二维数组。
    也兼容个别版本改成的 ``{"event":..,"data":..}`` 形态——两者的共同点是
    「事件名 + 数据」，所以按这两种形态读，而不假设只有一种。
    """
    if not isinstance(messages, list):
        return ""
    for item in messages:
        event: object = None
        data: object = None
        if isinstance(item, (list, tuple)) and len(item) == 2:
            event, data = item
        elif isinstance(item, dict):
            event, data = item.get("event"), item.get("data")
        else:
            continue
        if event != "execution_error" or not isinstance(data, dict):
            continue
        exception_type = str(data.get("exception_type", ""))
        exception_message = str(data.get("exception_message", ""))
        return f"{exception_type}: {exception_message}".strip(": ")
    return ""


def _is_resource_exhaustion(detail: str) -> bool:
    lowered = detail.lower()
    return any(marker in lowered for marker in _RESOURCE_EXHAUSTION_MARKERS)


def _require_component(value: object, *, field: str, allow_empty: bool = False) -> str:
    """校验 /history 给出的文件定位片段。

    它们会被拼进 ``/view`` 的查询参数。虽然 ComfyUI 自己处理这些参数，但
    ``..`` / 绝对路径 / 内嵌协议这类值说明响应已经不可信了——此时拒绝比
    「让下游自己小心」更安全。
    """
    if not isinstance(value, str):
        raise GatewayRejectedError(f"/history 的 {field} 缺失或不是字符串")
    if not value:
        if allow_empty:
            return ""
        raise GatewayRejectedError(f"/history 的 {field} 为空")
    if (
        "\x00" in value
        or ".." in value
        or "://" in value
        or value.startswith(("/", "\\"))
    ):
        raise GatewayRejectedError(
            f"/history 的 {field}={value!r} 形态异常（含穿越、绝对路径或内嵌协议）"
        )
    return value
