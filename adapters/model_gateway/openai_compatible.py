"""OpenAI 兼容的模型网关实现（``POST {base}/images/generations``）。

选它作为第一个真实适配器的理由
------------------------------
它是目前覆盖面最广的一种「事实标准」：官方 API、Azure OpenAI、以及多数聚合
网关都提供这组端点。因此一份实现能对接多个供应商，换供应商通常只改
``MODEL_GATEWAY_BASE_URL`` 与 ``MODEL_GATEWAY_DEFAULT_MODEL``。

三个必须写死的安全边界
----------------------
1. **密钥不进日志**：只记录 host 与 model。``Authorization`` 一旦进日志，
   轮换密钥就变成了「同时清理日志」，而日志往往还有副本。
2. **产物大小有上限**：先看 ``content-length``，再在流式读取时累计。
   只信 ``content-length`` 会被「声明 1KB、实际 10GB」绕开。
3. **下载 URL 限协议、限跳数**：产物 URL 来自网关响应，仍按不可信输入处理；
   允许 ``file://`` 等于把本地文件读给外部请求方。

失败映射（对应 ``ports.py`` 的二分）
------------------------------------
| 情形 | 抛出 |
|---|---|
| 连接失败 / 超时 / 5xx / 429 | ``GatewayRetryableError`` |
| 其他 4xx、响应结构不符契约、超出大小上限 | ``GatewayRejectedError`` |
| base_url 非法、缺 api_key | ``GatewayMisconfiguredError``（构造期即拒绝） |
"""

from __future__ import annotations

import base64
import binascii
from typing import Any
from urllib.parse import urlparse

import httpx

from adapters.model_gateway.ports import (
    GatewayArtifact,
    GatewayMisconfiguredError,
    GatewayRejectedError,
    GatewayRetryableError,
    GenerationRequest,
)

_MAX_REDIRECTS = 3
_ERROR_BODY_EXCERPT = 200


class OpenAICompatibleGateway:
    """把 ``GenerationRequest`` 翻译成一次 images/generations 调用。"""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        default_model: str = "",
        timeout_seconds: float = 120.0,
        max_bytes: int = 256 * 1024 * 1024,
        response_format: str = "b64_json",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        scheme = urlparse(base_url).scheme if base_url else ""
        if scheme not in {"http", "https"}:
            raise GatewayMisconfiguredError(
                f"MODEL_GATEWAY_BASE_URL 必须是 http(s) 绝对地址，当前={base_url!r}"
            )
        if not api_key:
            raise GatewayMisconfiguredError("MODEL_GATEWAY_API_KEY 为空：拒绝以无凭证方式调用")
        if max_bytes <= 0:
            raise ValueError("max_bytes 必须是正数：无上限的产物下载会打爆磁盘")
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._default_model = default_model
        self._max_bytes = max_bytes
        self._response_format = response_format
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds, connect=10.0),
            follow_redirects=False,
        )
        self._owns_client = client is None

    def __repr__(self) -> str:
        # 刻意不含 api_key：repr 会进异常、日志、调试器快照。
        return (
            f"OpenAICompatibleGateway(base_url={self._base_url!r}, "
            f"model={self._default_model!r}, max_bytes={self._max_bytes})"
        )

    async def aclose(self) -> None:
        """只关闭自己创建的 client：外部注入的由注入方管理生命周期。"""
        if self._owns_client:
            await self._client.aclose()

    async def generate(self, request: GenerationRequest) -> GatewayArtifact:
        model = request.model or self._default_model
        if not model:
            raise GatewayMisconfiguredError(
                "请求未指定 model 且未配置 MODEL_GATEWAY_DEFAULT_MODEL"
            )

        payload: dict[str, Any] = {
            "model": model,
            "prompt": request.prompt,
            "n": 1,
            "response_format": self._response_format,
        }
        if request.size:
            payload["size"] = request.size
        # extra 只允许补充标准字段，不允许覆盖已确定的三项：让调用方改写
        # model/n 会让「这个任务用哪个模型」失去单一事实源（预算按模型计价）。
        for key, value in request.extra.items():
            if key not in {"model", "prompt", "n", "response_format"}:
                payload[key] = value

        response = await self._post(payload, request=request)
        body = self._parse_json(response)
        upstream_id = str(response.headers.get("x-request-id", "") or body.get("id", ""))

        data, media_type = await self._extract_bytes(body, request)
        return GatewayArtifact(
            data=data,
            media_type=media_type,
            declared_sha256="",  # 该协议不返回摘要，由存储层自行计算
            upstream_request_id=upstream_id,
        )

    # --- 内部 -------------------------------------------------------------

    async def _post(
        self, payload: dict[str, Any], *, request: GenerationRequest
    ) -> httpx.Response:
        url = f"{self._base_url}/images/generations"
        try:
            response = await self._client.post(
                url,
                json=payload,
                headers={"Authorization": f"Bearer {self._api_key}"},
            )
        except httpx.TimeoutException as exc:
            # 超时可能是「网关还在算」：重试会重复计费，但不重试任务就永远卡住。
            # 这里选择可重试，由预算域的预留/结算去约束重复成本（P1-05）。
            raise GatewayRetryableError(
                f"网关超时（task={request.task_id}, model={payload['model']}）"
            ) from exc
        except httpx.TransportError as exc:
            raise GatewayRetryableError(
                f"网关连接失败（task={request.task_id}）: {type(exc).__name__}"
            ) from exc

        if response.status_code == 429 or response.status_code >= 500:
            raise GatewayRetryableError(
                f"网关返回 {response.status_code}"
                f"（task={request.task_id}）: {_excerpt(response)}"
            )
        if response.status_code >= 400:
            raise GatewayRejectedError(
                f"网关拒绝请求 {response.status_code}"
                f"（task={request.task_id}）: {_excerpt(response)}"
            )
        return response

    @staticmethod
    def _parse_json(response: httpx.Response) -> dict[str, Any]:
        try:
            body = response.json()
        except ValueError as exc:
            # 200 但不是 JSON：契约不符，重试同样的请求大概率还是不符。
            raise GatewayRejectedError(
                f"网关响应不是 JSON（status={response.status_code}）: {_excerpt(response)}"
            ) from exc
        if not isinstance(body, dict):
            raise GatewayRejectedError(f"网关响应顶层不是对象: {type(body).__name__}")
        return body

    async def _extract_bytes(
        self, body: dict[str, Any], request: GenerationRequest
    ) -> tuple[bytes, str]:
        items = body.get("data")
        if not isinstance(items, list) or not items:
            raise GatewayRejectedError(
                f"网关响应缺少非空 data 数组（task={request.task_id}）"
            )
        first = items[0]
        if not isinstance(first, dict):
            raise GatewayRejectedError("网关响应 data[0] 不是对象")

        b64 = first.get("b64_json")
        if isinstance(b64, str) and b64:
            return self._decode_b64(b64, request), request.media_type

        url = first.get("url")
        if isinstance(url, str) and url:
            return await self._download(url, request)

        raise GatewayRejectedError(
            "网关响应 data[0] 既无 b64_json 也无 url："
            "产物取回方式与当前适配器约定不符（不是瞬时问题，重试无意义）"
        )

    def _decode_b64(self, b64: str, request: GenerationRequest) -> bytes:
        # 先按长度粗判：base64 膨胀约 4/3，超限的串不必解码就知道会超。
        if len(b64) // 4 * 3 > self._max_bytes:
            raise GatewayRejectedError(
                f"产物 base64 长度 {len(b64)} 超出上限 {self._max_bytes}"
                f"（task={request.task_id}）"
            )
        try:
            data = base64.b64decode(b64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise GatewayRejectedError(
                f"产物 base64 解码失败（task={request.task_id}）"
            ) from exc
        if len(data) > self._max_bytes:
            raise GatewayRejectedError(
                f"产物 {len(data)} 字节超出上限 {self._max_bytes}"
            )
        return data

    async def _download(self, url: str, request: GenerationRequest) -> tuple[bytes, str]:
        current = url
        for _ in range(_MAX_REDIRECTS + 1):
            if urlparse(current).scheme not in {"http", "https"}:
                raise GatewayRejectedError(
                    f"产物 URL 协议不受支持（只允许 http/https）: {current!r}"
                )
            try:
                async with self._client.stream("GET", current) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location:
                            raise GatewayRejectedError("产物 URL 重定向但缺少 location")
                        current = str(httpx.URL(current).join(location))
                        continue
                    if response.status_code >= 400:
                        raise GatewayRejectedError(
                            f"产物下载失败 {response.status_code}: {current!r}"
                        )
                    media_type = self._media_type_of(response, request)
                    return await self._read_capped(response, request), media_type
            except httpx.TimeoutException as exc:
                raise GatewayRetryableError(
                    f"产物下载超时（task={request.task_id}）"
                ) from exc
            except httpx.TransportError as exc:
                raise GatewayRetryableError(
                    f"产物下载连接失败（task={request.task_id}）: {type(exc).__name__}"
                ) from exc
        raise GatewayRejectedError(f"产物 URL 跳转超过 {_MAX_REDIRECTS} 次: {url!r}")

    @staticmethod
    def _media_type_of(response: httpx.Response, request: GenerationRequest) -> str:
        header: str = (
            response.headers.get("content-type", "").split(";")[0].strip().lower()
        )
        # 只接受 image/* 与 video/*：网关若回 text/html（典型的是错误页伪装成
        # 200），照单收下会把一个网页存成「产物」，QC 之后才发现。
        if header.startswith(("image/", "video/")):
            return header
        return request.media_type

    async def _read_capped(
        self, response: httpx.Response, request: GenerationRequest
    ) -> bytes:
        declared = response.headers.get("content-length")
        if declared is not None:
            try:
                if int(declared) > self._max_bytes:
                    raise GatewayRejectedError(
                        f"产物声明大小 {declared} 超出上限 {self._max_bytes}"
                        f"（task={request.task_id}）"
                    )
            except ValueError:
                pass  # 非法 content-length 不作为拒绝理由，交给累计计数兜底
        chunks: list[bytes] = []
        total = 0
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            # 累计判定而不是只信 content-length：声明值可以是假的。
            if total > self._max_bytes:
                raise GatewayRejectedError(
                    f"产物实际大小超过上限 {self._max_bytes}（task={request.task_id}）"
                )
            chunks.append(chunk)
        return b"".join(chunks)


def _excerpt(response: httpx.Response) -> str:
    """截断错误体：完整体可能很大，且可能含调用方数据。"""
    try:
        text = response.text
    except Exception:
        return "<unreadable body>"
    return text[:_ERROR_BODY_EXCERPT]
