"""Outbox 事件处理器注册表（按 ``event_type`` 细分的下游接入点）。

这里为空是**如实的状态**：目前没有按事件类型分流的消费者。
统一外发由 ``__main__`` 按配置装配的兜底处理器（webhook）承担：
``OUTBOX_WEBHOOK_URL`` 为空 → 没有兜底 → 事件保持未投递、积压可见。

接入细分消费者（如 generation worker）时只改这里：按 ``event_type`` 注册异步
函数，收到 ``OutboxEvent`` 并完成投递（必须按 ``event_id`` 幂等）。
专用处理器优先于兜底处理器——这样可以对某类事件走不同通道。
"""

from __future__ import annotations

from workers.relay.consumer import EventHandler

HANDLERS: dict[str, EventHandler] = {}
