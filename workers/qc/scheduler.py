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

单个租户失败**不阻断其他租户**（对标 ``workers/relay`` 的逐租户隔离）：一个
租户的数据坏了、或者它的事务超时，不该让整批租户一起饿着。但这种「部分失败」
同样不能折叠成「干净空闲」——它必须作为 ``tenant_errors`` 上报，让一次性运行
以非零退出码说明「这一轮不完整」。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

logger = logging.getLogger(__name__)

_DISCOVER_SQL = "SELECT tenant_id, due_count FROM qc_maintenance.enumerate_due_tasks()"


class DiscoveryIdentityError(RuntimeError):
    """发现身份的能力面与声明不符（被扩权、或误配成超级用户）。"""


@dataclass(frozen=True)
class CycleOutcome:
    """一轮委派的结果：三个维度分开，不用一个布尔值折叠。

    ``blocked``：发现不可用，本轮什么都没做（不是「没有活」）。
    ``busy``：至少一个租户有实质工作。
    ``tenant_errors``：逐租户隔离后仍失败的租户数（本轮不完整）。
    """

    blocked: bool = False
    busy: bool = False
    tenant_errors: int = 0

    @property
    def degraded(self) -> bool:
        return self.tenant_errors > 0


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
            logger.exception("QC tenant discovery failed; treat as blocked and claim nothing")
            return None
        # due_count 为 0 的租户不该进入后续路径：让它们进来只会为每条空租户
        # 付一次租户事务，把「没有活」放大成 N 次查询。
        return [row.tenant_id for row in rows if int(row.due_count) > 0]


# 发现身份**必须**不能执行的函数：维护命令一旦可执行，发现身份就不再是
# 「只发现」——它能以 owner 身份改任务状态。这里逐个点名而不是「除发现函数
# 外全部」，因为断言要和授权面一一对得上，才可能在扩权时立刻炸出来。
_FORBIDDEN_FOR_DISCOVERY: tuple[str, ...] = (
    "qc_maintenance.reconcile_tenant_tasks(text)",
    "qc_maintenance.repair_tenant_task(text, text, text)",
    "qc_maintenance.detect_projection_anomalies()",
)

_IDENTITY_SQL = text(
    """
    SELECT
        current_user AS role,
        (SELECT rolsuper FROM pg_roles WHERE rolname = current_user) AS is_superuser,
        (SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user) AS bypass_rls
    """
)


async def verify_discovery_identity(engine: AsyncEngine) -> None:
    """启动期校验发现身份：不可提权、不可执行维护命令。

    为什么要在启动期做：授权面是**迁移期**事实，运行期的进程看不到它。若有人
    在别的迁移里给 ``qc_scheduler`` 补了一句 ``GRANT``，进程会照常启动、照常
    「只发现」，直到某天有人顺着这条路径改了任务状态。把声明变成启动期断言，
    扩权就变成启动失败，而不是一个需要靠人回忆的约定。
    """
    async with engine.connect() as conn:
        identity = (await conn.execute(_IDENTITY_SQL)).one()
        problems: list[str] = []
        if identity.is_superuser:
            problems.append("发现身份是超级用户：任何授权面都拦不住它")
        if identity.bypass_rls:
            problems.append("发现身份带 BYPASSRLS：全域读能力与「只发现」互斥")
        for function in _FORBIDDEN_FOR_DISCOVERY:
            allowed = await conn.scalar(
                text("SELECT has_function_privilege(current_user, :f, 'EXECUTE')"),
                {"f": function},
            )
            if allowed:
                problems.append(f"发现身份可执行维护命令 {function}")
        # 发现函数本身必须可执行，否则「能发现」只是纸面结论。
        can_discover = await conn.scalar(
            text(
                "SELECT has_function_privilege(current_user, "
                "'qc_maintenance.enumerate_due_tasks()', 'EXECUTE')"
            )
        )
        if not can_discover:
            problems.append("发现身份不能执行 enumerate_due_tasks()：无从调度")
    if problems:
        raise DiscoveryIdentityError(
            f"调度发现身份 {identity.role} 与声明不符：" + "；".join(problems)
        )


class TenantScheduler:
    """按租户把 QC 工作委派给 Worker（领取/执行/回收都由 Worker 负责）。"""

    def __init__(self, *, discovery: TenantDiscovery, worker: Any) -> None:
        self._discovery = discovery
        self._worker = worker

    async def run_once(self) -> CycleOutcome:
        """一轮委派；发现不可用返回 ``blocked``，单租户失败只记 ``tenant_errors``。"""
        tenants = await self._discovery.tenants()
        if tenants is None:
            # 阻断：不领取、不清扫。清扫同样依赖租户列表，用旧列表去回收会让
            # 「发现不可用」期间的状态漂移扩大而不是收敛。
            return CycleOutcome(blocked=True)
        busy = False
        errors = 0
        for tenant in tenants:
            try:
                if await self._worker.process_one(tenant=tenant):
                    busy = True
                if await self._worker.sweep_expired(tenant=tenant):
                    busy = True
            except Exception:
                # 单租户失败隔离：记下来、继续下一个。直接把异常抛出去会让
                # 「一个租户坏了」升级成「整批不调度」，而循环里再统一 catch
                # 又会把「部分失败」吞成「空闲」。
                errors += 1
                logger.exception(
                    "QC tenant delegation failed; continue with other tenants",
                    extra={"tenant": str(tenant)},
                )
        return CycleOutcome(busy=busy, tenant_errors=errors)

    async def sweep(self) -> int | None:
        """按租户回收过期租约；发现不可用时返回 ``None``（阻断，非「回收 0 条」）。"""
        tenants = await self._discovery.tenants()
        if tenants is None:
            return None
        reclaimed = 0
        for tenant in tenants:
            try:
                reclaimed += await self._worker.sweep_expired(tenant=tenant)
            except Exception:
                logger.exception(
                    "QC lease sweep failed for tenant; continue",
                    extra={"tenant": str(tenant)},
                )
        return reclaimed

    async def run_forever(self, *, idle_sleep: float) -> None:
        while True:
            try:
                outcome = await self.run_once()
            except Exception:
                logger.exception("QC tenant scheduler cycle failed")
                outcome = CycleOutcome()
            if not outcome.busy and not outcome.degraded:
                await asyncio.sleep(idle_sleep)


__all__ = [
    "CycleOutcome",
    "DiscoveryIdentityError",
    "MaintenanceTenantDiscovery",
    "TenantDiscovery",
    "TenantScheduler",
    "verify_discovery_identity",
]
