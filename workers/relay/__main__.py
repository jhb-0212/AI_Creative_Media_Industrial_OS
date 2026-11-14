"""Relay 进程入口：``python -m workers.relay``。

- 循环节奏与批量大小来自配置（OUTBOX_RELAY_INTERVAL_SECONDS / BATCH_SIZE）。
- SIGTERM / SIGINT 触发优雅退出：置位 stop 事件，等在途一轮结束后再关闭引擎，
  避免把「已投递未 ack」的事务半途掐断（那会变成本可避免的重复投递）。
- 进程只起引擎与会话工厂，不起 API：relay 与 API 共用同一镜像、同一 app_role。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import signal

from packages.shared.config import get_settings
from packages.shared.database import create_engine, create_sessionmaker
from packages.shared.logging import get_logger, setup_logging
from workers.relay.consumer import OutboxRelay
from workers.relay.handlers import HANDLERS

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
    engine = create_engine()
    sessionmaker = create_sessionmaker(engine)
    relay = OutboxRelay(sessionmaker, HANDLERS, batch_size=batch_size)
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
            )
        else:
            await _run_forever(relay, interval, stop)
    finally:
        # 先停投递再释放连接池：连接池关闭时在途事务会被强断。
        await engine.dispose()


def main() -> None:
    """命令行入口。"""
    settings = get_settings()
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
