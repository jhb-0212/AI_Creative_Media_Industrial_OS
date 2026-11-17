"""生成执行进程入口：``python -m workers.generation``。

执行上下文由 ``GENERATION_EXECUTOR_CONTEXTS`` 环境变量提供，格式与
``DELIVERY_CONSUMER_CONTEXTS`` 相同：``<tenant_id>:<org_id>:<projects>``，
多条用逗号分隔；``<projects>`` 是 ``|`` 分隔的项目 id 列表，``*`` 表示
该组织内全部项目。范围是**授权**，来自编排层配置，不从任务行自称。

适配器选择（``GENERATION_ADAPTER``，默认 deterministic）：
- ``deterministic``：确定性适配器（Phase 1，不调用外部供应商，ADR-R32）；
- 真实供应商适配随 model_gateway 域落地后在此注册新选择。

两个域连接池（ADR-R33）：执行环本体走 generation_execution 角色；产出登记
必须走 asset_service 角色（跨域写不许借别人的连接）。两者在进程退出时都要
释放——只关一个是资源泄漏，而且泄漏的正是被高频使用的那个池。

worker 身份（ADR-R34）：``hostname:pid``。租约归属按它区分，因此同一台机器
上第二个进程必须得到不同的 pid——用固定字符串会让「谁在执行」失去意义。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import socket
import uuid

from packages.shared.config import get_settings
from packages.shared.database import create_engine, create_sessionmaker
from packages.shared.db_roles import DatabaseDomain
from packages.shared.logging import get_logger, setup_logging
from workers.generation.adapter import DeterministicGenerationAdapter
from workers.generation.asset_registrar import AssetVersionRegistrar
from workers.generation.executor import (
    GenerationExecutorContext,
    GenerationRoundStats,
    execute_round,
)

logger = get_logger(__name__)


def _parse_project_scope(raw: str) -> frozenset[str]:
    """把 projects 字段解析为项目范围（空串一律 fail-closed，不默认全部）。"""
    parts = [p.strip() for p in raw.split("|") if p.strip()]
    return frozenset(parts)


def _worker_id() -> str:
    """进程内唯一的 worker 标识（租约归属与终态栅栏的依据）。

    不用主机名单独做标识：同一台机器跑两个 worker 是常态，两者共用一个
    标识会让接管后的旧进程绕过栅栏。pid 让它们可区分。
    """
    return f"{socket.gethostname()}:{os.getpid()}"


def _contexts(worker_id: str) -> tuple[GenerationExecutorContext, ...]:
    """从环境变量解析受信的执行上下文（解析失败宁可启动失败不静默空转）。"""
    raw = os.environ.get("GENERATION_EXECUTOR_CONTEXTS", "").strip()
    if not raw:
        return ()
    contexts: list[GenerationExecutorContext] = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        fields = entry.split(":")
        if len(fields) != 3:
            raise ValueError(
                f"GENERATION_EXECUTOR_CONTEXTS 条目格式错误（应为 tenant:org:projects）: {entry!r}"
            )
        tenant_raw, org_raw, projects_raw = (f.strip() for f in fields)
        project_ids = _parse_project_scope(projects_raw)
        if not project_ids:
            raise ValueError(
                f"GENERATION_EXECUTOR_CONTEXTS 条目缺少项目范围（拒绝默认全部）: {entry!r}"
            )
        contexts.append(
            GenerationExecutorContext(
                tenant_id=uuid.UUID(tenant_raw),
                organization_id=uuid.UUID(org_raw),
                project_ids=project_ids,
                worker_id=worker_id,
            )
        )
    return tuple(contexts)


async def _main_async(*, once: bool) -> None:
    settings = get_settings()
    contexts = _contexts(_worker_id())
    if not contexts:
        logger.warning(
            "generation_executor_no_contexts",
            hint="未设置 GENERATION_EXECUTOR_CONTEXTS：无任务会被执行",
        )
        return
    engine = create_engine(DatabaseDomain.GENERATION_EXECUTION)
    sessionmaker = create_sessionmaker(engine)
    asset_engine = create_engine(DatabaseDomain.ASSET)
    asset_sessionmaker = create_sessionmaker(asset_engine)
    adapter = DeterministicGenerationAdapter()
    registrar = AssetVersionRegistrar(asset_sessionmaker=asset_sessionmaker)
    lease_seconds = settings.generation_lease_seconds
    try:
        while True:
            total = GenerationRoundStats()
            for context in contexts:
                stats = await execute_round(
                    sessionmaker,
                    context,
                    adapter,
                    total,
                    registrar=registrar,
                    lease_seconds=lease_seconds,
                )
                logger.info(
                    "generation_executor_round",
                    tenant_id=str(context.tenant_id),
                    worker_id=context.worker_id,
                    claimed=stats.claimed,
                    reclaimed=stats.reclaimed,
                    succeeded=stats.succeeded,
                    failed=stats.failed,
                    skipped=stats.skipped,
                )
            if once:
                break
            await asyncio.sleep(settings.outbox_relay_interval_seconds)
    finally:
        await asset_engine.dispose()
        await engine.dispose()


def main() -> None:
    setup_logging()
    parser = argparse.ArgumentParser(description="生成任务执行 worker")
    parser.add_argument("--once", action="store_true", help="只跑一轮后退出")
    args = parser.parse_args()
    asyncio.run(_main_async(once=args.once))


if __name__ == "__main__":
    main()
