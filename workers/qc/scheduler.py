"""调度发现 → 租户委派（WP-B 的最小闭环）。

为什么发现与执行必须是两个身份、两段逻辑
------------------------------------------
「知道哪些租户有待检任务」与「能读/改某个租户的任务」是两种能力。把它们放在
同一个连接里，最常见的结果是：为了发现而给进程全域读权限，然后为了让这个
权限「不白给」，又在发现路径上顺手读任务内容。权限一旦合并就再也回不去。

所以本模块只做两件事：

1. 用**调度发现身份**调用零参发现函数，得到「租户 + 待检条数」；
2. 对每个租户，把工作交给 QC Worker，由 QC 池在租户上下文里领取/执行/回收。

失败语义
--------
发现失败时**不领取任何任务**，也不静默当成「没有活」：空结果只有两种合法
含义——健康且确无候选，或系统阻断且已告警。异常必须上报为阻断，否则「调度
发现挂了」会表现为「队列很空」，是最难发现的一类停摆。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

logger = logging.getLogger(__name__)

_DISCOVER_SQL = (
    "SELECT tenant_id, due_count FROM qc_maintenance.enumerate_due_tasks()"
)


class TenantDiscovery(Protocol):
    """发现协议：返回需要处理的租户列表；无法判定时返回 ``None``（阻断）。"""

    async def tenants(self) -> list[Any] | None: ...


class MaintenanceTenantDiscovery:
    """经调度发现身份读取「租户 + 待检条数」。

    ``engine`` 必须是 ``qc_scheduler`` 身份（见 ``create_qc_scheduler_engine``）。
    这里不做任何 ``SET ROLE``/``SET SESSION AUTHORIZATION``：发现能力的边界由
    数据库授权面决定，适配层不做身份模拟——模拟出来的边界等于没有边界。
    """

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def tenants(self) -> list[Any] | None:
        try:
            # 只读函数，不需要事务提交；``connect()`` 不持有写事务。
            async with self._engine.connect() as conn:
                rows = (await conn.execute(text(_DISCOVER_SQL))).all()
        except Exception:
            logger.exception(
                "QC tenant discovery failed; treat as blocked and claim nothing"
            )
            return None
        # due_count 为 0 的租户不该进入后续路径：让它们进来只会为每条空租户
        # 付一次租户事务，把「没有活」放大成 N 次查询。
        return [row.tenant_id for row in rows if int(row.due_count) > 0]


class TenantScheduler:
    """按租户把 QC 工作委派给 Worker（领取/执行/回收都由 Worker 负责）。"""

    def __init__(self, *, discovery: TenantDiscovery, worker: Any) -> None:
        self._discovery = discovery
        self._worker = worker

    async def run_once(self) -> bool | None:
        """一轮委派：``True`` 有活 / ``False`` 干净空闲 / ``None`` 阻断。

        三种结果必须可区分。把阻断折叠成 ``False`` 和「队列很空」共用同一个
        出口，调用方就只能二选一地误读：要么把停摆当空闲（漏报），要么把空闲
        当停摆（误报）。返回值分开以后，``--once`` 能把阻断映射成非零退出码。
        """
        tenants = await self._discovery.tenants()
        if tenants is None:
            # 阻断：不领取、不清扫。清扫同样依赖租户列表，用旧列表去回收会让
            # 「发现不可用」期间的状态漂移扩大而不是收敛。
            return None
        busy = False
        for tenant in tenants:
            if await self._worker.process_one(tenant=tenant):
                busy = True
            if await self._worker.sweep_expired(tenant=tenant):
                busy = True
        return busy

    async def sweep(self) -> int | None:
        """按租户回收过期租约；发现不可用时返回 ``None``（阻断，非「回收 0 条」）。"""
        tenants = await self._discovery.tenants()
        if tenants is None:
            return None
        reclaimed = 0
        for tenant in tenants:
            reclaimed += await self._worker.sweep_expired(tenant=tenant)
        return reclaimed

    async def run_forever(self, *, idle_sleep: float) -> None:
        while True:
            try:
                busy = await self.run_once()
            except Exception:
                logger.exception("QC tenant scheduler cycle failed")
                busy = False
            if not busy:
                await asyncio.sleep(idle_sleep)


__all__ = ["MaintenanceTenantDiscovery", "TenantDiscovery", "TenantScheduler"]
