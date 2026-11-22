"""生成 Worker 内核（R16 Phase 2 §3）。

职责边界：只负责「领任务 → 心跳续租 → 回写终态 → 回收过期租约」这套调度。
真正的生成动作由注入的 ``GenerationExecutor`` 完成，Worker 不碰模型、不碰供应商。

调度语义：
- 每轮从仓储取**最早创建**的 REQUESTED 任务（FIFO，防止新任务把老任务饿死）；
- ``claim`` 走服务层，状态迁移与事件都由领域对象自己完成，Worker 不直改状态；
- 执行期间按租约 1/3 周期续租；续租失败即视为租约丢失，取消执行、放弃回写；
- 执行抛异常也要落终态 FAILED，不能让任务卡在 RUNNING 等别人扫；
- 过期扫尾只处理「RUNNING 且 lease_expires_at < now」，回收失败（已被别人回收/并发冲突）
  吞掉并继续，不当成致命错误。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from packages.errors.error_codes import DomainError

logger = logging.getLogger(__name__)

OutcomeResult = Literal["SUCCEEDED", "FAILED"]


@dataclass(frozen=True)
class GenerationOutcome:
    """执行结果：Worker 只认这两种终态，中间态由领域状态机管。"""

    result: OutcomeResult
    evidence_ref: str = ""


class GenerationExecutor(Protocol):
    """执行器协议。实现方负责把任务变成产物，并给出可供审计的证据引用。"""

    async def execute(self, task: Any) -> GenerationOutcome: ...


class LeaseLostError(RuntimeError):
    """租约在心跳期间丢失（被回收或续租冲突），执行结果作废。"""


class GenerationWorker:
    """常驻生成 Worker。单进程串行领取，多进程靠租约互斥。"""

    def __init__(
        self,
        *,
        service: Any,
        repository: Any,
        executor: GenerationExecutor,
        worker_id: str,
        lease_seconds: int = 60,
        idle_sleep: float = 1.0,
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
        """租约 1/3 周期心跳：留两次重试余量才到期。"""
        return max(1.0, self.lease_seconds / 3)

    async def process_one(self) -> bool:
        """领一个任务并跑完。返回 False 表示当前没有可领任务。"""
        task = await self._repository.next_claimable()
        if task is None:
            return False
        claimed = await self._service.claim(
            task_id=task.entity_id,
            worker_id=self.worker_id,
            lease_seconds=self.lease_seconds,
        )
        try:
            outcome = await self._execute_with_heartbeat(claimed)
        except LeaseLostError:
            logger.warning(
                "lease lost while running task %s, abandon result", claimed.entity_id
            )
            return True
        except Exception as exc:  # 执行器把异常抛出来也必须落终态
            logger.exception("executor failed for task %s", claimed.entity_id)
            outcome = GenerationOutcome(
                result="FAILED",
                evidence_ref=f"worker-error:{type(exc).__name__}",
            )
        try:
            await self._service.finish(
                task_id=claimed.entity_id,
                worker_id=self.worker_id,
                result=outcome.result,
                evidence_ref=outcome.evidence_ref,
            )
        except DomainError as exc:
            # 执行期间租约被别人回收：结果作废，状态以回收方为准，不能覆盖。
            logger.warning("finish rejected for task %s: %s", claimed.entity_id, exc)
        return True

    async def _execute_with_heartbeat(self, task: Any) -> GenerationOutcome:
        work = asyncio.create_task(self._executor.execute(task))
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
                    raise LeaseLostError(
                        f"renew denied for {task.entity_id}: {exc}"
                    ) from exc
        finally:
            if not work.done():
                work.cancel()
                await asyncio.gather(work, return_exceptions=True)

    async def sweep_expired(self) -> int:
        """回收过期 RUNNING 任务，返回成功回收条数。"""
        expired = await self._repository.list_expired(_now())
        reclaimed = 0
        for task in expired:
            try:
                await self._service.release_expired(task_id=task.entity_id)
            except DomainError as exc:
                # 别人先回收了 / 状态已变：不是错误，跳过即可
                logger.info("skip reclaim %s: %s", task.entity_id, exc)
                continue
            reclaimed += 1
        return reclaimed

    async def run_once(self) -> bool:
        """一轮调度：优先领任务，空闲时顺带扫尾。返回是否领到了任务。"""
        if await self.process_one():
            return True
        reclaimed = await self.sweep_expired()
        if reclaimed:
            logger.info("reclaimed %d expired lease(s)", reclaimed)
        return False

    async def run_forever(self) -> None:
        """常驻循环：单轮异常只记日志，不退出进程。"""
        while True:
            try:
                busy = await self.run_once()
            except Exception:
                logger.exception("generation worker cycle failed")
                busy = False
            if not busy:
                await asyncio.sleep(self.idle_sleep)


def _now() -> datetime:
    return datetime.now(UTC)


__all__ = [
    "GenerationExecutor",
    "GenerationOutcome",
    "GenerationWorker",
    "LeaseLostError",
    "OutcomeResult",
]
