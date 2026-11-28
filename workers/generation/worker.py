"""生成 Worker 内核（R16 Phase 2 §3）。

职责边界：只负责「领任务 → 心跳续租 → 回写终态 → 回收过期租约」这套调度。
真正的生成动作由注入的 ``GenerationExecutor`` 完成，Worker 不碰模型、不碰供应商。

事务边界（P1-01 重构的核心）
---------------------------
**每个阶段一个短工作单元，各自提交**：

- 领取：``SKIP LOCKED`` 读候选 → claim → 立即 commit。租约在这一刻成为其他会话
  可见的事实，行锁同时释放；执行阶段不持有任何数据库事务。
- 心跳：renew → commit，独立短事务。
- 终结：finish → commit，独立短事务。
- 回收：逐条 release_expired → commit。

为什么必须这样：原实现把四个阶段塞进一个事务，执行期间租约未提交（其他 Worker
仍看到 REQUESTED，以为任务没人领），行锁也一直被持有（其他 Worker 领不到后续
任务）。「加锁」修不了，只能重画事务边界。

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
from packages.shared.lease import LeaseScopeFactory

logger = logging.getLogger(__name__)

OutcomeResult = Literal["SUCCEEDED", "FAILED"]


@dataclass(frozen=True)
class GenerationOutcome:
    """执行结果：Worker 只认这两种终态，中间态由领域状态机管。

    C7（单一入队事实源）：成功不再携带资产 ID / 质量版本。QC 待检项由
    ``GENERATION_SUCCEEDED`` 事件经 Relay → QC Handler 入队，而不是 Worker 在
    终态后另开一条直写 QC 表的路径。产物引用的权威来源始终是 T2 回执。
    """

    result: OutcomeResult
    evidence_ref: str = ""


class GenerationExecutor(Protocol):
    """执行器协议。实现方负责把任务变成产物，并给出可供审计的证据引用。"""

    async def execute(self, task: Any) -> GenerationOutcome: ...


class LeaseLostError(RuntimeError):
    """租约在心跳期间丢失（被回收或续租冲突），执行结果作废。"""


class GenerationWorker:
    """常驻生成 Worker。单进程串行领取，多进程靠租约 + SKIP LOCKED 并行。"""

    def __init__(
        self,
        *,
        scope: LeaseScopeFactory,
        executor: GenerationExecutor,
        worker_id: str,
        lease_seconds: int = 60,
        idle_sleep: float = 1.0,
    ) -> None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds 必须为正")
        self._scope = scope
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
        claimed = await self._claim_next()
        if claimed is None:
            return False
        try:
            outcome = await self._execute_with_heartbeat(claimed)
        except LeaseLostError:
            logger.warning("lease lost while running task %s, abandon result", claimed.entity_id)
            return True
        # R42：SUCCEEDED 不是 Worker 可写的终态。执行器成功意味着 T1 已提交
        # AWAITING 与登记请求；终态由 relay 的 T2/T3 依据资产回执推进。
        # 只有失败仍由持租约的 Worker 就地登记（GENERATION.PROCESS 边）。
        if outcome.result == "FAILED":
            await self._finish(claimed, outcome)
        return True

    # ---- 四个阶段各自成事务 ------------------------------------------------ #

    async def _claim_next(self) -> Any | None:
        """短事务领取：读候选 → claim → 立即提交，把租约变成可见事实。"""
        async with self._scope() as unit:
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
                # 行已被锁住却仍 claim 失败：状态在读到之后被改掉了（例如刚被终结）。
                # 这不是故障，交回主循环下一轮重试即可，不能让整轮崩掉。
                logger.info("skip claim %s: %s", candidate.entity_id, exc)
                return None
            await unit.commit()
            return claimed

    async def _renew(self, task_id: str) -> None:
        async with self._scope() as unit:
            await unit.service.renew(
                task_id=task_id,
                worker_id=self.worker_id,
                lease_seconds=self.lease_seconds,
            )
            await unit.commit()

    async def _finish(self, task: Any, outcome: GenerationOutcome) -> bool:
        async with self._scope() as unit:
            try:
                await unit.service.finish(
                    task_id=task.entity_id,
                    worker_id=self.worker_id,
                    result="FAILED",
                    evidence_ref=outcome.evidence_ref,
                )
            except DomainError as exc:
                # 执行期间租约被别人回收：结果作废，状态以回收方为准，不能覆盖。
                logger.warning("finish rejected for task %s: %s", task.entity_id, exc)
                return False
            await unit.commit()
        # QC 入队**不在这里做**：GENERATION_SUCCEEDED 随终态在同一事务写入 outbox，
        # 由 relay 消费后入队（workers/relay/handlers.py）。Worker 不持有 QC 会话，
        # 「一个业务结果、一个入队事实源」——漏投递由 relay 重试与死信处置负责，
        # 不再从生成侧另开直写 QC 表的入口。
        return True

    async def _execute_with_heartbeat(self, task: Any) -> GenerationOutcome:
        work = asyncio.create_task(self._executor.execute(task))
        try:
            while True:
                done, _ = await asyncio.wait({work}, timeout=self.heartbeat_interval)
                if work in done:
                    return work.result()
                try:
                    await self._renew(task.entity_id)
                except DomainError as exc:
                    raise LeaseLostError(f"renew denied for {task.entity_id}: {exc}") from exc
        except LeaseLostError:
            raise
        except Exception as exc:  # 执行器把异常抛出来也必须落终态
            logger.exception("executor failed for task %s", task.entity_id)
            return GenerationOutcome(
                result="FAILED",
                evidence_ref=f"worker-error:{type(exc).__name__}",
            )
        finally:
            if not work.done():
                work.cancel()
                await asyncio.gather(work, return_exceptions=True)

    async def sweep_expired(self) -> int:
        """回收过期 RUNNING 任务，返回成功回收条数（逐条独立事务）。"""
        async with self._scope() as unit:
            expired = [task.entity_id for task in await unit.repository.list_expired(_now())]
        reclaimed = 0
        for task_id in expired:
            async with self._scope() as unit:
                try:
                    await unit.service.release_expired(task_id=task_id)
                    await unit.commit()
                except DomainError as exc:
                    # 别人先回收了 / 状态已变：不是错误，跳过即可
                    logger.info("skip reclaim %s: %s", task_id, exc)
                    continue
            reclaimed += 1
        return reclaimed

    async def run_once(self) -> bool:
        """一轮调度：优先领任务，空闲时扫尾回收租约。返回是否领到了任务。

        C7：QC 入队不在本 Worker 的职责内——它由 GENERATION_SUCCEEDED 事件经
        Relay 单一路径完成；漏投递是 Relay 的重试/死信问题，不从生成侧补第二入口。
        """
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
