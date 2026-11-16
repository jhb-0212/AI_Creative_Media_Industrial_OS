"""Webhook 外发投递测试（R06 下游）。

三条必须成立，否则「已投递」没有可信度：
1. 幂等键就是 `event_id`（收方能按它去重）；
2. 签名可被收方用同一套算法复算（时间戳进签名，防重放）；
3. 非 2xx 必须抛错 → relay 不 ack → 下一轮重试。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from datetime import UTC, datetime

import httpx
import pytest

from workers.relay.consumer import OutboxEvent
from workers.relay.errors import DeliveryDeferredError
from workers.relay.webhook import (
    WebhookCircuitOpenError,
    WebhookDeliveryError,
    WebhookDispatcher,
)

SECRET = "s3cret-for-test"


def _event() -> OutboxEvent:
    return OutboxEvent(
        event_id=uuid.uuid4(),
        aggregate_type="ScriptRevision",
        aggregate_id="agg-1",
        tenant_id=uuid.uuid4(),
        event_type="SCRIPT_LOCK_APPROVED",
        payload={"to_status": "LOCKED", "reason": "复审通过"},
        occurred_at=datetime.now(UTC),
    )


async def test_dispatch_carries_verifiable_signature_and_idempotency_key() -> None:
    captured: dict[str, httpx.Request] = {}

    async def receiver(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return httpx.Response(200)

    dispatcher = WebhookDispatcher(
        "https://example.test/hook", SECRET, transport=httpx.MockTransport(receiver)
    )
    event = _event()
    try:
        await dispatcher(event)
    finally:
        await dispatcher.aclose()

    request = captured["request"]
    assert request.headers["Idempotency-Key"] == str(event.event_id)
    assert request.headers["X-Outbox-Event-Type"] == event.event_type

    # 收方视角复算签名：时间戳 + 原始 body 用同一密钥做 HMAC-SHA256。
    timestamp = request.headers["X-Outbox-Timestamp"]
    expected = hmac.new(
        SECRET.encode("utf-8"),
        f"{timestamp}.".encode() + request.content,
        hashlib.sha256,
    ).hexdigest()
    assert request.headers["X-Outbox-Signature"] == expected

    body = json.loads(request.content)
    assert body["event_id"] == str(event.event_id)
    assert body["tenant_id"] == str(event.tenant_id)
    assert body["payload"]["reason"] == "复审通过"


async def test_non_2xx_raises_so_relay_keeps_event_unpublished() -> None:
    async def receiver(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    dispatcher = WebhookDispatcher(
        "https://example.test/hook", SECRET, transport=httpx.MockTransport(receiver)
    )
    try:
        with pytest.raises(WebhookDeliveryError):
            await dispatcher(_event())
    finally:
        await dispatcher.aclose()


async def test_transport_error_raises() -> None:
    async def receiver(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    dispatcher = WebhookDispatcher(
        "https://example.test/hook", SECRET, transport=httpx.MockTransport(receiver)
    )
    try:
        with pytest.raises(WebhookDeliveryError):
            await dispatcher(_event())
    finally:
        await dispatcher.aclose()


def test_empty_secret_refuses_to_construct() -> None:
    """无密钥就不构造：拒绝发送任何人都能伪造的事件。"""
    with pytest.raises(ValueError):
        WebhookDispatcher("https://example.test/hook", "")


async def test_circuit_open_fast_fails_without_sending() -> None:
    """熔断 OPEN 时快速失败，不真正外发（R9）。

    与 test_circuit_open_blocks_all_further_deliveries 的分工：
    这里断言「快速失败抛的是 WebhookCircuitOpenError 子类、可被
    WebhookDeliveryError 捕获」，后者断言「外发次数不再增长」。
    """
    calls = 0

    async def receiver(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503)

    dispatcher = WebhookDispatcher(
        "https://example.test/hook",
        SECRET,
        transport=httpx.MockTransport(receiver),
        circuit_failure_threshold=1,
        circuit_recovery_seconds=30.0,
    )
    try:
        # 第一次请求 503 → 记失败 → OPEN。
        with pytest.raises(WebhookDeliveryError):
            await dispatcher(_event())

        # 熔断 OPEN：抛的是 WebhookCircuitOpenError，且它仍是
        # WebhookDeliveryError 的子类（relay 的 except Exception 能接住）。
        with pytest.raises(WebhookCircuitOpenError):
            await dispatcher(_event())
    finally:
        await dispatcher.aclose()

    assert calls == 1  # 第二次被熔断拦截，未真正外发。


async def test_circuit_open_blocks_all_further_deliveries() -> None:
    """熔断 OPEN 后，任意事件都不再外发，直到冷却期满半开探测（R9）。"""
    calls = 0

    async def receiver(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503)

    dispatcher = WebhookDispatcher(
        "https://example.test/hook",
        SECRET,
        transport=httpx.MockTransport(receiver),
        circuit_failure_threshold=1,
        circuit_recovery_seconds=30.0,
    )
    try:
        with pytest.raises(WebhookDeliveryError):
            await dispatcher(_event())
        assert calls == 1

        # 后续三条事件全部被熔断快速失败，不增加外发次数。
        for _ in range(3):
            with pytest.raises(WebhookCircuitOpenError):
                await dispatcher(_event())
        assert calls == 1
    finally:
        await dispatcher.aclose()


# --------------------------------------------------------------------------- #
# 熔断语义分层（审查 N1 / N2）
# --------------------------------------------------------------------------- #
async def test_threshold_zero_disables_circuit_without_crashing() -> None:
    """配置口径「阈值 0 = 关闭熔断」必须可构造且真的永不熔断（N1）。"""
    calls = 0

    async def receiver(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503)

    dispatcher = WebhookDispatcher(
        "https://example.test/hook",
        SECRET,
        transport=httpx.MockTransport(receiver),
        circuit_failure_threshold=0,
        circuit_enabled=False,
    )
    try:
        # 关闭熔断：每条事件都真的外发，不会出现 WebhookCircuitOpenError。
        for expected in (1, 2, 3):
            with pytest.raises(WebhookDeliveryError):
                await dispatcher(_event())
            assert calls == expected
    finally:
        await dispatcher.aclose()


async def test_client_error_does_not_open_circuit() -> None:
    """4xx 是事件级失败，不能把端点打成 OPEN（N2：一条毒事件不该误伤全体）。"""
    calls = 0

    async def receiver(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(400, json={"error": "payload 不合法"})

    dispatcher = WebhookDispatcher(
        "https://example.test/hook",
        SECRET,
        transport=httpx.MockTransport(receiver),
        circuit_failure_threshold=1,
        circuit_recovery_seconds=30.0,
    )
    try:
        # 阈值 1：若把 4xx 记成端点失败，第二次就会熔断拦截、calls 停在 1。
        for expected in (1, 2, 3):
            with pytest.raises(WebhookDeliveryError) as excinfo:
                await dispatcher(_event())
            assert not isinstance(excinfo.value, WebhookCircuitOpenError)
            assert calls == expected
    finally:
        await dispatcher.aclose()


async def test_circuit_open_is_delivery_deferred() -> None:
    """熔断拦截必须被标记为「未外发」，relay 才不会把它记成一次失败尝试（N2）。"""

    async def receiver(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    dispatcher = WebhookDispatcher(
        "https://example.test/hook",
        SECRET,
        transport=httpx.MockTransport(receiver),
        circuit_failure_threshold=1,
    )
    try:
        with pytest.raises(WebhookDeliveryError):
            await dispatcher(_event())
        with pytest.raises(WebhookCircuitOpenError) as excinfo:
            await dispatcher(_event())
    finally:
        await dispatcher.aclose()

    # 双重身份：既是投递异常（既有调用方可捕获），也是「未外发」信号。
    assert isinstance(excinfo.value, WebhookDeliveryError)
    assert isinstance(excinfo.value, DeliveryDeferredError)
