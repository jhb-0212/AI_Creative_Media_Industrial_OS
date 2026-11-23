"""Relay 进程入口：``python -m workers.relay``。

- 循环节奏与批量大小来自配置（OUTBOX_RELAY_INTERVAL_SECONDS / BATCH_SIZE）。
- SIGTERM / SIGINT 触发优雅退出：置位 stop 事件，等在途一轮结束后再关闭引擎，
  避免把「已投递未 ack」的事务半途掐断（那会变成本可避免的重复投递）。
- 进程只起引擎与会话工厂，不起 API。relay 以专用角色 outbox_relay_role 连接
  （F03 数据所有权）：该角色仅持有 outbox schema 的 SELECT + UPDATE，不持有
  业务 schema 写权限，越界写业务表在数据库层被拒绝。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import signal

from packages.shared.config import get_process_settings
from packages.shared.database import (
    create_qc_governance_engine,
    create_relay_engine,
    create_sessionmaker,
)
from packages.shared.logging import get_logger, setup_logging
from workers.relay.consumer import OutboxRelay
from workers.relay.handlers import build_handlers
from workers.relay.webhook import WebhookDispatcher

logger = get_logger(__name__)


async def _run_forever(relay: OutboxRelay, interval: float, stop: asyncio.Event) -> None:
    """循环投递，直到 stop 置位。单轮异常不让进程退出（否则一次抖动静默停摆）。"""
    while not stop.is_set():
        try:
            await relay.run_once()
        except Exception:
            logger.exception("outbox_relay_round_failed")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval)


async def _main_async(*, interval: float, batch_size: int, once: bool) -> None:
    settings = get_process_settings("relay")
    # relay 用专用角色 outbox_relay_role 连接（非 app_role），数据库层隔离其
    # 越界写业务表的可能。引擎、DSN 角色与连接池参数都来自**同一份** relay
    # 进程配置（create_relay_engine(settings)）——不在入口另复制一遍 pool 参数，
    # 也不让 DSN 构造回落到 api 角色的全局配置（审查 P1-04 / R7 的根因）。
    engine = create_relay_engine(settings)
    sessionmaker = create_sessionmaker(engine)
    # 业务域处理器需要**自己的**连接角色：relay 角色只有 outbox 的 SELECT/UPDATE，
    # 写 qc_tasks 必须用 qc_governance 角色（ADR-R06 方案 B 的刻意隔离）。
    qc_engine = create_qc_governance_engine()
    qc_sessionmaker = create_sessionmaker(qc_engine)
    handlers = build_handlers(qc_sessionmaker=qc_sessionmaker)
    # 兜底处理器：配了 webhook 才存在。没配时事件保持未投递（积压可见），
    # 而不是被「成功」丢弃。
    # 熔断开关：配置口径「阈值 0 = 关闭熔断」，因此 enabled 由阈值派生；原值原样
    # 传入由 CircuitBreaker 在关闭态归一化（审查 N1：不能因 0 而启动崩溃）。
    dispatcher = (
        WebhookDispatcher(
            settings.outbox_webhook_url,
            settings.outbox_webhook_secret.get_secret_value(),
            timeout_seconds=settings.outbox_webhook_timeout_seconds,
            circuit_failure_threshold=settings.outbox_webhook_circuit_failure_threshold,
            circuit_recovery_seconds=settings.outbox_webhook_circuit_recovery_seconds,
            circuit_enabled=settings.outbox_webhook_circuit_failure_threshold > 0,
        )
        if settings.outbox_webhook_url
        else None
    )
    relay = OutboxRelay(
        sessionmaker, handlers, batch_size=batch_size, default_handler=dispatcher
    )
    if dispatcher is None:
        logger.warning(
            "outbox_relay_no_downstream",
            hint="未配置 OUTBOX_WEBHOOK_URL：事件将保持未投递，积压会持续增长",
        )
    stop = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        # Windows 上 add_signal_handler 不支持 SIGTERM，失败时退化为默认行为。
        with contextlib.suppress(NotImplementedError, ValueError):
            loop.add_signal_handler(sig, stop.set)

    try:
        if once:
            stats = await relay.run_once()
            logger.info(
                "outbox_relay_once",
                tenants=stats.tenants_scanned,
                delivered=stats.delivered,
                unrouted=stats.unrouted,
                failed=stats.failed,
                deferred=stats.deferred,
            )
        else:
            await _run_forever(relay, interval, stop)
    finally:
        # 先停投递再释放连接池：连接池关闭时在途事务会被强断。
        if dispatcher is not None:
            await dispatcher.aclose()
        await qc_engine.dispose()
        await engine.dispose()


def main() -> None:
    """命令行入口。"""
    settings = get_process_settings("relay")
    parser = argparse.ArgumentParser(description="Outbox 事件投递消费端")
    parser.add_argument(
        "--interval",
        type=float,
        default=settings.outbox_relay_interval_seconds,
        help="两轮之间的间隔秒数",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=settings.outbox_relay_batch_size,
        help="单租户单轮最多领取的事件数",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="只跑一轮后退出（用于本地验证与一次性排空）",
    )
    args = parser.parse_args()

    setup_logging(settings.log_level)
    asyncio.run(
        _main_async(
            interval=args.interval, batch_size=args.batch_size, once=args.once
        )
    )


if __name__ == "__main__":
    main()
