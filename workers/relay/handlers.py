"""Outbox 事件处理器注册表（下游接入的唯一位置）。

现在为空，这是**如实的状态**而不是遗漏：
- Phase 1 还没有需要接收事件的下游（无 Kafka / 无 webhook / 无数仓）。
- 没注册处理器的事件不会被 ack，积压会持续增长并由 ``outbox_relay_backlog``
  暴露出来——这是「没有下游」唯一诚实的表达方式。若这里塞一个空操作的
  处理器把事件标成已投递，等于静默丢事件。

接入下游时只改这里：按 ``event_type`` 注册一个异步函数，
收到 ``OutboxEvent`` 并完成投递即可（必须幂等，按 ``event_id`` 去重）。
"""

from __future__ import annotations

from workers.relay.consumer import EventHandler

HANDLERS: dict[str, EventHandler] = {}
