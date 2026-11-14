"""Outbox 事件的外发投递（webhook 派发）。

它是 relay 的**下游**，职责单一：把一条事件可靠地送出去，成功/失败如实返回。

为什么先做 webhook，而不是直接上 Kafka：
- Kafka 需要 broker、客户端库、分区/消费组与偏移量管理——当前没有真实消费者，
  先建一套消息基础设施只会得到一个没人读的 topic；
- webhook 是「配置即启用」的投递通道：URL 为空时不注册处理器，事件保持未投递；
  URL 一配就真的外发，行为不依赖任何未落地的组件。

三条硬约定：
1. **不重试**。失败直接抛异常，由 relay 保留 `published_at` 为空、下一轮重试。
   重试策略只有一处（relay 的轮次），这里再塞一层退避会让「到底重试了几次」
   变得无法解释。
2. **幂等键 = event_id**（`Idempotency-Key` 头）。relay 是至少一次语义，收方
   必须能按这个键去重；把键放在最显眼的标准头上，比写在 body 里更不容易漏。
3. **签名**：`X-Outbox-Signature = HMAC-SHA256(secret, "{timestamp}.{body}")`。
   时间戳进签名，收方可拒绝过旧请求，避免签名被无限期重放。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any

import httpx

from workers.relay.consumer import OutboxEvent


class WebhookDeliveryError(RuntimeError):
    """一次外发未成功（非 2xx 或传输层失败）。

    继承 RuntimeError 而不是定义一堆状态码分支：relay 只关心「成功 / 未成功」，
    细分由日志与 HTTP 状态码承担。
    """


class WebhookDispatcher:
    """把事件 POST 到配置的端点，带签名与幂等键。

    ``transport`` 仅用于测试注入 ``httpx.MockTransport``；生产为 None（真实网络）。
    """

    def __init__(
        self,
        url: str,
        secret: str,
        *,
        timeout_seconds: float = 5.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not secret:
            raise ValueError("webhook 密钥为空：拒绝发送未签名的事件")
        self._url = url
        self._secret = secret.encode("utf-8")
        self._client = httpx.AsyncClient(timeout=timeout_seconds, transport=transport)

    async def __call__(self, event: OutboxEvent) -> None:
        """投递一条事件；未成功即抛 ``WebhookDeliveryError``。"""
        body = self._encode(event)
        timestamp = str(int(time.time()))
        headers = {
            "Content-Type": "application/json",
            "Idempotency-Key": str(event.event_id),
            "X-Outbox-Event-Type": event.event_type,
            "X-Outbox-Tenant-Id": str(event.tenant_id),
            "X-Outbox-Timestamp": timestamp,
            "X-Outbox-Signature": self._sign(timestamp, body),
        }
        try:
            response = await self._client.post(self._url, content=body, headers=headers)
        except httpx.HTTPError as exc:
            raise WebhookDeliveryError(f"webhook 传输失败：{exc}") from exc
        if response.status_code >= 300:
            raise WebhookDeliveryError(
                f"webhook 返回 {response.status_code}（期望 2xx）"
            )

    async def aclose(self) -> None:
        """关闭连接池（relay 退出时调用）。"""
        await self._client.aclose()

    def _encode(self, event: OutboxEvent) -> bytes:
        """序列化为稳定字节：键排序 + 紧凑分隔符，保证签名可被收方复算。

        用 ``default=str`` 兜住 datetime/UUID：payload 里出现非 JSON 原生类型时
        不静默失败，而是可预期的字符串化。
        """
        envelope: dict[str, Any] = {
            "event_id": str(event.event_id),
            "tenant_id": str(event.tenant_id),
            "aggregate_type": event.aggregate_type,
            "aggregate_id": event.aggregate_id,
            "event_type": event.event_type,
            "occurred_at": event.occurred_at.isoformat(),
            "payload": event.payload,
        }
        return json.dumps(
            envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
        ).encode("utf-8")

    def _sign(self, timestamp: str, body: bytes) -> str:
        """HMAC-SHA256 十六进制签名（时间戳参与签名）。"""
        message = f"{timestamp}.".encode() + body
        return hmac.new(self._secret, message, hashlib.sha256).hexdigest()
