"""Delivery 消费进程入口：``python -m workers.delivery``。

租户列表由 ``DELIVERY_CONSUMER_TENANT_IDS`` 环境变量提供（逗号分隔的 UUID），
不擅自扩大资产域的跨租户枚举权限——消费范围由编排层显式决定。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import uuid

from packages.shared.config import get_settings
from packages.shared.database import create_engine, create_sessionmaker
from packages.shared.db_roles import DatabaseDomain
from packages.shared.logging import get_logger, setup_logging
from workers.delivery.consumer import run_once

logger = get_logger(__name__)


def _tenant_ids() -> tuple[uuid.UUID, ...]:
    raw = os.environ.get("DELIVERY_CONSUMER_TENANT_IDS", "").strip()
    if not raw:
        return ()
    return tuple(uuid.UUID(part.strip()) for part in raw.split(",") if part.strip())


async def _main_async(*, once: bool) -> None:
    tenant_ids = _tenant_ids()
    if not tenant_ids:
        logger.warning(
            "delivery_consumer_no_tenants",
            hint="未设置 DELIVERY_CONSUMER_TENANT_IDS：无租户会被消费",
        )
        return
    engine = create_engine(DatabaseDomain.ASSET)
    sessionmaker = create_sessionmaker(engine)
    try:
        while True:
            stats = await run_once(sessionmaker, tenant_ids=tenant_ids)
            logger.info(
                "delivery_consumer_round",
                tenants=stats.tenants_scanned,
                events=stats.events_seen,
                promoted=stats.promoted,
                skipped=stats.skipped,
                failed=stats.failed,
            )
            if once:
                break
            await asyncio.sleep(get_settings().outbox_relay_interval_seconds)
    finally:
        await engine.dispose()


def main() -> None:
    setup_logging()
    parser = argparse.ArgumentParser(description="交付清单审批事件消费端")
    parser.add_argument("--once", action="store_true", help="只跑一轮后退出")
    args = parser.parse_args()
    asyncio.run(_main_async(once=args.once))


if __name__ == "__main__":
    main()
