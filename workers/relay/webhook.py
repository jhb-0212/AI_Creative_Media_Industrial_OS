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

端点熔断（R9）：``CircuitBreaker`` 在发请求前拦截——连续失败达到阈值后 OPEN，
后续请求快速失败（不真正外发），冷却期满放行一个半开探测。它与上面的「不重试」
不冲突：熔断失败同样抛异常、由 relay 统一走退避/死信，只是「不撞墙」提前到
发请求之前。

端点故障 vs 事件故障（审查 N2 的分层，别把两者混为一谈）：
- **端点级失败**（传输错误、5xx、408/425/429）说明「下游整体不行了」，计入熔断；
- **事件级失败**（其余 4xx：签名错、payload 不合法、单条被拒）说明「这条事件不行」，
  端点本身是活的，**不计入端点熔断**，否则一条毒事件重试几次就能把端点打成 OPEN，
  误伤全部健康事件。
被熔断拦截时抛的是 ``DeliveryDeferredError`` 子类——**本次没有外发**，relay 因此
不会把它计成一次失败尝试（否则死信会吞掉从未尝试过的事件）。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any

import httpx

from packages.shared.circuit_breaker import CircuitBreaker, CircuitState
from packages.shared.metrics import OUTBOX_WEBHOOK_CIRCUIT_STATE, observe_circuit_state
from workers.relay.consumer import OutboxEvent
from workers.relay.errors import DeliveryDeferredError, OutboxDeliveryError


def _observe_endpoint_circuit(state: CircuitState, previous: CircuitState | None) -> None:
    """把端点熔断状态发布到 webhook 专属 Gauge。

    接口在 ``CircuitBreaker`` 里，指标名在这里——因为只有这里知道
    「这个熔断器管的是 webhook 端点」。
    """
    observe_circuit_state(
        OUTBOX_WEBHOOK_CIRCUIT_STATE,
        state.value,
        previous.value if previous is not None else None,
    )


class WebhookDeliveryError(OutboxDeliveryError):
    """一次外发未成功（非 2xx 或传输层失败）。

    继承 ``OutboxDeliveryError`` 而不是定义一堆状态码分支：relay 只关心
    「成功 / 未成功」，细分由日志与 HTTP 状态码承担。
    """


class WebhookCircuitOpenError(WebhookDeliveryError, DeliveryDeferredError):
    """熔断器处于 OPEN：请求被快速失败，未真正外发。

    同时是 ``WebhookDeliveryError``（既有调用方按投递异常捕获不受影响）与
    ``DeliveryDeferredError``（relay 据此**不递增 attempts**）。两个父类表达的正是
    这件事的两面：投递没成功，但也不是「又失败了一次」。
    """


# 端点级失败的状态码：传输错误已在调用点单独处理，这里补「端点活着但整体不可用」
# 的几种应答。5xx = 下游自身故障；408/425/429 = 下游要求稍后重试（限流/超时）。
_ENDPOINT_FAILURE_STATUSES = frozenset({408, 425, 429})


def is_endpoint_failure(status_code: int) -> bool:
    """该状态码是否代表「端点级故障」（唯一判定口径，供熔断与测试共用）。"""
    return status_code >= 500 or status_code in _ENDPOINT_FAILURE_STATUSES


class WebhookDispatcher:
    """把事件 POST 到配置的端点，带签名与幂等键，端点级熔断兜底。

    ``transport`` 仅用于测试注入 ``httpx.MockTransport``；生产为 None（真实网络）。
    ``breaker`` 用于测试注入假时钟熔断器；为 None 时按 ``circuit_enabled`` 构造。
    """

    def __init__(
        self,
        url: str,
        secret: str,
        *,
        timeout_seconds: float = 5.0,
        transport: httpx.AsyncBaseTransport | None = None,
        breaker: CircuitBreaker | None = None,
        circuit_failure_threshold: int = 5,
        circuit_recovery_seconds: float = 30.0,
        circuit_enabled: bool = True,
    ) -> None:
        if not secret:
            raise ValueError("webhook 密钥为空：拒绝发送未签名的事件")
        self._url = url
        self._secret = secret.encode("utf-8")
        self._client = httpx.AsyncClient(timeout=timeout_seconds, transport=transport)
        # 熔断器：注入的 breaker 优先（测试可控时钟/状态）；否则按参数构造。
        self._breaker = (
            breaker
            if breaker is not None
            else CircuitBreaker(
                failure_threshold=circuit_failure_threshold,
                recovery_timeout_seconds=circuit_recovery_seconds,
                enabled=circuit_enabled,
                state_observer=_observe_endpoint_circuit,
            )
        )

    async def __call__(self, event: OutboxEvent) -> None:
        """投递一条事件；未成功即抛 ``WebhookDeliveryError``。

        熔断器 OPEN 时直接抛 ``WebhookCircuitOpenError``（快速失败，不真正外发）。
        """
        if not self._breaker.allow():
            raise WebhookCircuitOpenError("webhook 端点熔断开启，快速失败（未外发）")
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
            # 传输层失败：请求可能根本没到对端，属于端点级故障。
            self._breaker.record_failure()
            raise WebhookDeliveryError(f"webhook 传输失败：{exc}") from exc
        if response.status_code >= 300:
            if is_endpoint_failure(response.status_code):
                self._breaker.record_failure()
                raise WebhookDeliveryError(
                    f"webhook 返回 {response.status_code}（端点级失败：已计入端点熔断）"
                )
            # 4xx 事件级失败：端点能应答，说明它是活的——清零连续失败，
            # 失败归属这条事件，交给 relay 的事件级退避/死信处理。
            self._breaker.record_success()
            raise WebhookDeliveryError(
                f"webhook 返回 {response.status_code}"
                "（事件级失败：不计入端点熔断，按事件退避/死信处理）"
            )
        self._breaker.record_success()

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
