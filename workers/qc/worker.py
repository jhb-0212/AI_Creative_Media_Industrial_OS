"""QC Worker 内核（Phase 3）：领取待检任务 → 心跳 → 判定 → 回收过期租约。

与生成 Worker 同构的调度语义，差别只有一处：检测器未配置时**不领取**任务。
理由：QC 的产出是「通过/不通过」的判定，占位检测器若随手给 PASSED，会把未检
的产物变成「已检通过」——比什么都不做危险得多。宁可让队列停住并明确告警，
也不制造假绿。

事务边界与生成侧一致（P1-01）：领取、心跳、判定、回收各自是短工作单元并立即
提交。检测本身（读资产文件、算哈希）不持有 QC 域事务；报告由资产域会话提交，
与判定是两次独立提交——这条跨域缺口由 P1-03 用事务 Outbox 解决，不在本文件里
用「共享一个 Session」的假原子掩盖。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from packages.errors.error_codes import DomainError
from packages.shared.lease import LeaseScope, LeaseScopeFactory

logger = logging.getLogger(__name__)

QCResult = Literal["PASSED", "REJECTED"]

# 按租户打开的作用域：调度发现只给出「哪个租户有活」，真正读任务必须在
# 该租户的 RLS 上下文里进行。两个工厂分开注入，是为了让「有没有设置上下文」
# 成为类型上的事实，而不是靠调用方记得先调 set_tenant_context。
TenantScopeFactory = Callable[[object], AbstractAsyncContextManager[LeaseScope]]


@dataclass(frozen=True)
class QCInspection:
    """一次检测的结论：必须有报告编号，报告与结论一一对应。"""

    result: QCResult
    qc_report_id: str
    evidence_refs: list[str] = field(default_factory=list)


class QCExecutor(Protocol):
    """检测器协议。``configured`` 为 False 时 Worker 不领取任何任务。"""

    configured: bool

    async def inspect(self, task: Any) -> QCInspection | None: ...


class QcLeaseLostError(RuntimeError):
    """心跳期间租约丢失，判定结果作废。"""


class QCWorker:
    def __init__(
        self,
        *,
        scope: LeaseScopeFactory,
        executor: QCExecutor,
        worker_id: str,
        lease_seconds: int = 120,
        idle_sleep: float = 2.0,
        scope_for_tenant: TenantScopeFactory | None = None,
    ) -> None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds 必须为正")
        self._scope = scope
        self._scope_for_tenant = scope_for_tenant
        self._executor = executor
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self.idle_sleep = idle_sleep

    @property
    def heartbeat_interval(self) -> float:
        return max(1.0, self.lease_seconds / 3)

    def _unit(self, tenant: object | None) -> AbstractAsyncContextManager[LeaseScope]:
        """按是否需要租户上下文选作用域；要租户却没配工厂是装配错误，直接报。"""
        if tenant is None:
            return self._scope()
        if self._scope_for_tenant is None:
            raise RuntimeError(
                "指定了租户但没有装配租户作用域：这样读 qc_tasks 会在 RLS 下读到 0 行，"
                "把「没装配」伪装成「没有任务」"
            )
        return self._scope_for_tenant(tenant)

    async def process_one(self, tenant: object | None = None) -> bool:
        if not self._executor.configured:
            logger.warning(
                "QC detector not configured; refusing to claim tasks "
                "(a placeholder PASS would be a fake green)"
            )
            return False
        claimed = await self._claim_next(tenant)
        if claimed is None:
            return False
        try:
            inspection = await self._inspect_with_heartbeat(tenant, claimed)
        except QcLeaseLostError:
            logger.warning(
                "lease lost while inspecting %s; abandon result", claimed.entity_id
            )
            return True
        except Exception:
            logger.exception("QC executor failed for %s", claimed.entity_id)
            # 不写假的 REJECTED 报告：让租约自然过期后退回 QUEUED，交给人看日志。
            return True
        if inspection is None:
            return True
        await self._finish(tenant, claimed, inspection)
        return True

    # ---- 四个阶段各自成事务 ------------------------------------------------ #

    async def _claim_next(self, tenant: object | None = None) -> Any | None:
        async with self._unit(tenant) as unit:
            candidate = await unit.repository.next_claimable_for_claim()
            if candidate is None:
                return None
            try:
                claimed = await unit.service.claim(
                    task_id=candidate.entity_id,
                    worker_id=self.worker_id,
                    lease_seconds=self.lease_seconds,
                )
            except DomainError as exc:
                logger.info("skip QC claim %s: %s", candidate.entity_id, exc)
                return None
            await unit.commit()
            return claimed

    async def _renew(
        self, tenant: object | None, task_id: str, lease_generation: int
    ) -> None:
        async with self._unit(tenant) as unit:
            await unit.service.renew(
                task_id=task_id,
                worker_id=self.worker_id,
                expected_lease_generation=lease_generation,
                lease_seconds=self.lease_seconds,
            )
            await unit.commit()

    async def _finish(
        self, tenant: object | None, task: Any, inspection: QCInspection
    ) -> bool:
        async with self._unit(tenant) as unit:
            try:
                await unit.service.finish(
                    task_id=task.entity_id,
                    worker_id=self.worker_id,
                    expected_lease_generation=task.lease_generation,
                    result=inspection.result,
                    qc_report_id=inspection.qc_report_id,
                    evidence_refs=inspection.evidence_refs,
                )
            except DomainError as exc:
                logger.warning(
                    "finish rejected for QC task %s: %s", task.entity_id, exc
                )
                return False
            await unit.commit()
            return True

    async def _inspect_with_heartbeat(
        self, tenant: object | None, task: Any
    ) -> QCInspection | None:
        work = asyncio.create_task(self._executor.inspect(task))
        try:
            while True:
                done, _ = await asyncio.wait({work}, timeout=self.heartbeat_interval)
                if work in done:
                    return work.result()
                try:
                    # 代次随领取一起缓存：心跳证明的是「我是这一代」，不是「我是这任务」。
                    await self._renew(tenant, task.entity_id, task.lease_generation)
                except DomainError as exc:
                    raise QcLeaseLostError(
                        f"renew denied for {task.entity_id}: {exc}"
                    ) from exc
        finally:
            if not work.done():
                work.cancel()
                await asyncio.gather(work, return_exceptions=True)

    async def sweep_expired(self, tenant: object | None = None) -> int:
        async with self._unit(tenant) as unit:
            expired = [task.entity_id for task in await unit.repository.list_expired(_now())]
        reclaimed = 0
        for task_id in expired:
            async with self._unit(tenant) as unit:
                try:
                    await unit.service.release_expired(task_id=task_id)
                    await unit.commit()
                except DomainError as exc:
                    logger.info("skip reclaim %s: %s", task_id, exc)
                    continue
            reclaimed += 1
        return reclaimed

    async def run_once(self) -> bool:
        if await self.process_one():
            return True
        reclaimed = await self.sweep_expired()
        if reclaimed:
            logger.info("reclaimed %d expired QC lease(s)", reclaimed)
        return False

    async def run_forever(self) -> None:
        while True:
            try:
                busy = await self.run_once()
            except Exception:
                logger.exception("QC worker cycle failed")
                busy = False
            if not busy:
                await asyncio.sleep(self.idle_sleep)


def _now() -> datetime:
    return datetime.now(UTC)


__all__ = [
    "QCExecutor",
    "QCInspection",
    "QCResult",
    "QCWorker",
    "QcLeaseLostError",
]
