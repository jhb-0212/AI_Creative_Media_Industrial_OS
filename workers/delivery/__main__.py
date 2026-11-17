"""Delivery 消费进程入口：``python -m workers.delivery``。

消费上下文由 ``DELIVERY_CONSUMER_CONTEXTS`` 环境变量提供。每个条目形如
``<tenant_id>:<org_id>:<projects>``，多条用逗号分隔；``<projects>`` 是 ``|``
分隔的项目 id 列表，``*`` 表示该组织内全部项目。

为什么用显式上下文而不是只给租户列表（R4-1）：
- 消费要读项目级资源表 ``asset_service.delivery_manifests``，其 restrictive
  project 策略要求 org + projects 同时注入；只给 tenant 会静默看到 0 行。
- 组织与项目范围是**授权**，必须来自编排层配置，不能从事件 payload 读取。

示例：
  DELIVERY_CONSUMER_CONTEXTS="00000000-...-d5a1:00000000-...-d5a1:*"
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
from workers.delivery.consumer import DeliveryConsumerContext, run_once

logger = get_logger(__name__)


def _parse_project_scope(raw: str) -> frozenset[str]:
    """把 projects 字段解析为项目范围（空串一律 fail-closed，不默认全部）。"""
    parts = [p.strip() for p in raw.split("|") if p.strip()]
    return frozenset(parts)


def _contexts() -> tuple[DeliveryConsumerContext, ...]:
    """从环境变量解析受信的消费上下文。

    解析失败（非法 UUID、缺字段、空 project scope）直接抛错：worker 是后台
    进程，配置错误宁可启动失败也不静默空转（静默空转正是 R4-1 的教训）。
    """
    raw = os.environ.get("DELIVERY_CONSUMER_CONTEXTS", "").strip()
    if not raw:
        return ()
    contexts: list[DeliveryConsumerContext] = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        fields = entry.split(":")
        if len(fields) != 3:
            raise ValueError(
                f"DELIVERY_CONSUMER_CONTEXTS 条目格式错误（应为 tenant:org:projects）: {entry!r}"
            )
        tenant_raw, org_raw, projects_raw = (f.strip() for f in fields)
        tenant_id = uuid.UUID(tenant_raw)
        org_id = uuid.UUID(org_raw)
        project_ids = _parse_project_scope(projects_raw)
        if not project_ids:
            raise ValueError(
                f"DELIVERY_CONSUMER_CONTEXTS 条目缺少项目范围（拒绝默认全部）: {entry!r}"
            )
        contexts.append(
            DeliveryConsumerContext(
                tenant_id=tenant_id,
                organization_id=org_id,
                project_ids=project_ids,
            )
        )
    return tuple(contexts)


async def _main_async(*, once: bool) -> None:
    contexts = _contexts()
    if not contexts:
        logger.warning(
            "delivery_consumer_no_contexts",
            hint="未设置 DELIVERY_CONSUMER_CONTEXTS：无租户会被消费",
        )
        return
    engine = create_engine(DatabaseDomain.ASSET)
    sessionmaker = create_sessionmaker(engine)
    try:
        while True:
            stats = await run_once(sessionmaker, contexts=contexts)
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
