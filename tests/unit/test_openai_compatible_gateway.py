"""OpenAI 兼容网关适配器的失败映射与安全边界测试。

为什么用 ``httpx.MockTransport`` 而不是打真网关：被测命题是「状态码/响应形态
如何映射成两类失败」，这是纯映射逻辑。用真网关反而把网络抖动引进来，让同一个
用例时而红时而绿——那是最坏的一种测试。真网关的连通性属于 A1 的验收内容，
不在这里。
"""

from __future__ import annotations

import base64
import json

import httpx
import pytest

from adapters.model_gateway.fake import FakeModelGateway, deterministic_png
from adapters.model_gateway.openai_compatible import OpenAICompatibleGateway
from adapters.model_gateway.ports import (
    GatewayMisconfiguredError,
    GatewayRejectedError,
    GatewayRetryableError,
    GenerationRequest,
)

_BASE = "https://gateway.example.com/v1"
_KEY = "sk-test-do-not-log"
_PNG = deterministic_png("unit", size_bytes=128)


def _request(**overrides) -> GenerationRequest:
    base = {
        "task_id": "gen-1",
        "prompt": "a lighthouse at dusk",
        "model": "image-model-1",
        "media_type": "image/png",
        "size": "1024x1024",
    }
    base.update(overrides)
    return GenerationRequest(**base)


def _gateway(handler, **kwargs) -> OpenAICompatibleGateway:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return OpenAICompatibleGateway(base_url=_BASE, api_key=_KEY, client=client, **kwargs)


async def test_b64_response_is_decoded() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"data": [{"b64_json": base64.b64encode(_PNG).decode()}]},
            headers={"x-request-id": "req-abc"},
        )

    gateway = _gateway(handler)
    artifact = await gateway.generate(_request())

    assert artifact.data == _PNG
    assert artifact.media_type == "image/png"
    assert artifact.upstream_request_id == "req-abc"
    assert seen["url"] == f"{_BASE}/images/generations"
    assert seen["auth"] == f"Bearer {_KEY}"
    assert seen["body"]["model"] == "image-model-1"


async def test_url_response_is_downloaded() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"data": [{"url": "https://cdn.example.com/a.png"}]})
        return httpx.Response(200, content=_PNG, headers={"content-type": "image/png"})

    artifact = await _gateway(handler).generate(_request())
    assert artifact.data == _PNG
    assert artifact.media_type == "image/png"


async def test_5xx_and_429_are_retryable() -> None:
    for status in (500, 503, 429):
        gateway = _gateway(lambda request, s=status: httpx.Response(s, text="busy"))
        with pytest.raises(GatewayRetryableError):
            await gateway.generate(_request())


async def test_client_error_is_rejected_not_retried() -> None:
    """4xx 是确定性失败：重试同样的请求只会重复烧钱。"""
    gateway = _gateway(lambda request: httpx.Response(400, text="bad prompt"))
    with pytest.raises(GatewayRejectedError, match="400"):
        await gateway.generate(_request())


async def test_timeout_is_retryable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("connect timeout")

    with pytest.raises(GatewayRetryableError, match="超时"):
        await _gateway(handler).generate(_request())


async def test_transport_error_is_retryable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with pytest.raises(GatewayRetryableError, match="连接失败"):
        await _gateway(handler).generate(_request())


async def test_non_json_response_is_rejected() -> None:
    gateway = _gateway(lambda request: httpx.Response(200, text="<html>oops</html>"))
    with pytest.raises(GatewayRejectedError, match="不是 JSON"):
        await gateway.generate(_request())


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"data": []},
        {"data": [{}]},
        {"data": ["not-an-object"]},
    ],
)
async def test_contract_violations_are_rejected(body: dict) -> None:
    gateway = _gateway(lambda request: httpx.Response(200, json=body))
    with pytest.raises(GatewayRejectedError):
        await gateway.generate(_request())


async def test_invalid_base64_is_rejected() -> None:
    gateway = _gateway(
        lambda request: httpx.Response(200, json={"data": [{"b64_json": "!!!not-b64!!!"}]})
    )
    with pytest.raises(GatewayRejectedError, match="base64"):
        await gateway.generate(_request())


async def test_oversize_declared_content_length_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"data": [{"url": "https://cdn.example.com/big.png"}]})
        return httpx.Response(200, content=b"x" * 64, headers={"content-length": "999999999"})

    with pytest.raises(GatewayRejectedError, match="超出上限"):
        await _gateway(handler, max_bytes=1024).generate(_request())


async def test_lying_content_length_is_caught_by_cumulative_count() -> None:
    """声明 4 字节、实际远超上限：只信 content-length 的实现会放行。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"data": [{"url": "https://cdn.example.com/c.png"}]})
        return httpx.Response(200, content=b"y" * 4096, headers={"content-length": "4"})

    with pytest.raises(GatewayRejectedError, match="实际大小超过上限"):
        await _gateway(handler, max_bytes=512).generate(_request())


async def test_non_http_artifact_url_is_rejected() -> None:
    """``file://`` 会让一次外部调用变成任意本地文件读取。"""
    gateway = _gateway(
        lambda request: httpx.Response(200, json={"data": [{"url": "file:///etc/passwd"}]})
    )
    with pytest.raises(GatewayRejectedError, match="协议"):
        await gateway.generate(_request())


async def test_redirect_is_followed_then_capped() -> None:
    hops: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"data": [{"url": "https://cdn.example.com/0"}]})
        hops.append(str(request.url))
        index = int(str(request.url).rsplit("/", 1)[-1])
        if index < 9:
            return httpx.Response(302, headers={"location": f"https://cdn.example.com/{index + 1}"})
        return httpx.Response(200, content=_PNG, headers={"content-type": "image/png"})

    with pytest.raises(GatewayRejectedError, match="跳转超过"):
        await _gateway(handler).generate(_request())
    assert len(hops) == 4  # 首次 + 3 次跳转上限


async def test_html_error_page_is_not_accepted_as_artifact() -> None:
    """网关把错误页以 200 返回时，media_type 不能被伪造成 image/png 之外的类型。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"data": [{"url": "https://cdn.example.com/e"}]})
        return httpx.Response(
            200, content=b"<html>error</html>", headers={"content-type": "text/html"}
        )

    artifact = await _gateway(handler).generate(_request())
    # 回落到请求声明的类型，而不是把 text/html 当成产物类型
    assert artifact.media_type == "image/png"


def test_missing_api_key_is_misconfigured_at_construction() -> None:
    with pytest.raises(GatewayMisconfiguredError, match="API_KEY"):
        OpenAICompatibleGateway(base_url=_BASE, api_key="")


@pytest.mark.parametrize("bad_url", ["", "ftp://gw/v1", "gateway.example.com/v1"])
def test_invalid_base_url_is_misconfigured(bad_url: str) -> None:
    with pytest.raises(GatewayMisconfiguredError, match="BASE_URL"):
        OpenAICompatibleGateway(base_url=bad_url, api_key=_KEY)


async def test_api_key_never_appears_in_repr_or_errors() -> None:
    """密钥泄进日志的问题无法事后补救：轮换时还要清理所有日志副本。"""
    gateway = _gateway(lambda request: httpx.Response(401, text="unauthorized"))
    assert _KEY not in repr(gateway)
    with pytest.raises(GatewayRejectedError) as excinfo:
        await gateway.generate(_request())
    assert _KEY not in str(excinfo.value)


async def test_extra_cannot_override_model() -> None:
    """预算按模型计价：让 extra 改写 model 会让计价失去事实源。"""
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(_PNG).decode()}]})

    await _gateway(handler).generate(_request(extra={"model": "cheaper-model", "seed": 7}))
    assert seen["body"]["model"] == "image-model-1"
    assert seen["body"]["seed"] == 7


async def test_fake_gateway_is_deterministic() -> None:
    gw = FakeModelGateway()
    first = await gw.generate(_request())
    second = await gw.generate(_request())
    assert first.data == second.data
    other = await gw.generate(_request(prompt="different prompt"))
    assert other.data != first.data
    assert len(gw.calls) == 3


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("timeout", GatewayRetryableError),
        ("server_error", GatewayRetryableError),
        ("rejected", GatewayRejectedError),
        ("oversize", GatewayRejectedError),
        ("misconfigured", GatewayMisconfiguredError),
    ],
)
async def test_fake_gateway_failure_modes(mode: str, expected: type[Exception]) -> None:
    gw = FakeModelGateway(fail_mode=mode)  # type: ignore[arg-type]
    with pytest.raises(expected):
        await gw.generate(_request())
