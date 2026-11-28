"""网关产物取回的两条共同边界：大小上限与媒体类型。

为什么单独成一个模块
--------------------
自研兼容网关的 ``url`` 下载与未来的其他协议实现都要做同一件事：把「响应体」
变成「有上限的字节」。这段逻辑里藏着两个不显眼的坑，任何一份实现漏掉一个
都会变成静默缺陷：

1. **只信 ``content-length`` 会被绕开**——声明 1KB、实际 10GB 是上游的一条
   合法响应，不是攻击也需要防（网关配置错、代理截断都可能造出来）。必须先看
   声明值做快速拒绝，再在流式读取时累计计数。
2. **媒体类型不能照单全收**——网关在出错时常常回 ``text/html`` 错误页并带
   200。照单收下会把一个网页存成「产物」，直到 QC 阶段才发现，而那时它
   已经进了资产台账。

抄两遍就会有一份走样，所以抽在这里。两份实现的差异只保留在各协议自己的
错误文案与「是否严格只收图像」上。
"""

from __future__ import annotations

import httpx

from adapters.model_gateway.ports import GatewayRejectedError

# 错误体截断长度：完整体可能很大，且可能含调用方数据。
_ERROR_BODY_EXCERPT = 200


def excerpt(response: httpx.Response) -> str:
    """截断错误体，只用于排障。"""
    try:
        text = response.text
    except Exception:  # 读不出响应体不该掩盖原始错误
        return "<unreadable body>"
    return text[:_ERROR_BODY_EXCERPT]


def declared_media_type(response: httpx.Response) -> str:
    """取 ``content-type`` 的主类型（去参数、小写）。读不出时返回空串。"""
    header: str = response.headers.get("content-type", "").split(";")[0].strip().lower()
    return header


def media_type_or_fallback(response: httpx.Response, fallback: str) -> str:
    """只接受 ``image/*`` 与 ``video/*``，其余回落到请求声明的类型。

    回落而非拒绝，是因为有些网关对产物返回 ``application/octet-stream``——
    那是「没告诉你类型」，不是「类型错了」。真正的类型错误由调用方用
    :func:`is_media_type` 另行判定：网关出错时回的是 HTML 错误页，必须拒绝。
    """
    header = declared_media_type(response)
    if header.startswith(("image/", "video/")):
        return header
    return fallback


def is_media_type(response: httpx.Response, prefix: str) -> bool:
    """响应类型是否以 ``prefix`` 开头（用于「必须真的是图/视频」的硬判定）。"""
    return declared_media_type(response).startswith(prefix)


async def read_capped(response: httpx.Response, *, limit: int, task_id: str) -> bytes:
    """流式读取响应体，超过 ``limit`` 立即拒绝。

    先看声明、再累计计数：声明值可以是假的，累计计数才是兜底。
    """
    declared = response.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > limit:
                raise GatewayRejectedError(
                    f"产物声明大小 {declared} 超出上限 {limit}（task={task_id}）"
                )
        except ValueError:
            pass  # 非法 content-length 不作为拒绝理由，交给累计计数兜底

    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > limit:
            raise GatewayRejectedError(f"产物实际大小超过上限 {limit}（task={task_id}）")
        chunks.append(chunk)
    return b"".join(chunks)
