"""QC Worker 内核（Phase 3）：领取待检任务 → 心跳 → 判定 → 回收过期租约。

与生成 Worker 同构的调度语义，差别只有一处：检测器未配置时**不领取**任务。
理由：QC 的产出是「通过/不通过」的判定，占位检测器若随手给 PASSED，会把未检
的产物变成「已检通过」——比什么都不做危险得多。宁可让队列停住并明确告警，
也不制造假绿。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from packages.errors.error_codes import DomainError

logger = logging.getLogger(__name__)

QCResult = Literal["PASSED", "REJECTED"]


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
        service: Any,
        repository: Any,
        executor: QCExecutor,
        worker_id: str,
        lease_seconds: int = 120,
        idle_sleep: float = 2.0,
    ) -> None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds 必须为正")
        self._service = service
        self._repository = repository
        self._executor = executor
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self.idle_sleep = idle_sleep

    @property
    def heartbeat_interval(self) -> float:
        return max(1.0, self.lease_seconds / 3)

    async def process_one(self) -> bool:
        if not self._executor.configured:
            logger.warning(
                "QC detector not configured; refusing to claim tasks "
                "(a placeholder PASS would be a fake green)"
            )
            return False
        task = await self._repository.next_claimable()
        if task is None:
            return False
        claimed = await self._service.claim(
            task_id=task.entity_id,
            worker_id=self.worker_id,
            lease_seconds=self.lease_seconds,
        )
        try:
            inspection = await self._inspect_with_heartbeat(claimed)
        except QcLeaseLostError:
            logger.warning("lease lost while inspecting %s; abandon result", claimed.entity_id)
            return True
        except Exception:
            logger.exception("QC executor failed for %s", claimed.entity_id)
            # 不写假的 REJECTED 报告：让租约自然过期后退回 QUEUED，交给人看日志。
            return True
        if inspection is None:
            return True
        try:
            await self._service.finish(
                task_id=claimed.entity_id,
                worker_id=self.worker_id,
                result=inspection.result,
                qc_report_id=inspection.qc_report_id,
                evidence_refs=inspection.evidence_refs,
            )
        except DomainError as exc:
            logger.warning("finish rejected for QC task %s: %s", claimed.entity_id, exc)
        return True

    async def _inspect_with_heartbeat(self, task: Any) -> QCInspection | None:
        work = asyncio.create_task(self._executor.inspect(task))
        try:
            while True:
                done, _ = await asyncio.wait({work}, timeout=self.heartbeat_interval)
                if work in done:
                    return work.result()
                try:
                    await self._service.renew(
                        task_id=task.entity_id,
                        worker_id=self.worker_id,
                        lease_seconds=self.lease_seconds,
                    )
                except DomainError as exc:
                    raise QcLeaseLostError(
                        f"renew denied for {task.entity_id}: {exc}"
                    ) from exc
        finally:
            if not work.done():
                work.cancel()
                await asyncio.gather(work, return_exceptions=True)

    async def sweep_expired(self) -> int:
        expired = await self._repository.list_expired(_now())
        reclaimed = 0
        for task in expired:
            try:
                await self._service.release_expired(task_id=task.entity_id)
            except DomainError as exc:
                logger.info("skip reclaim %s: %s", task.entity_id, exc)
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
