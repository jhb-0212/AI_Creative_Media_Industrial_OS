"""GenerationTask 仓储接口（抽象，数据所有权先行）。

属于 generation-execution 模块的专属数据边界（V2.0.5 14.2 / A7 矩阵）：
GenerationTask 表只有本域角色 svc_generation_execution 可写。

``save`` 携带 expected_version 做乐观并发控制：worker 领取（PENDING→RUNNING）
是竞争点，两个 worker 同时领取同一任务时只有一个能成功，失败方收到
CONCURRENT_MODIFICATION（幂等跳过，9.4 Worker 崩溃恢复的幂等语义）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from services.generation_execution.domain.generation_task import GenerationTask


class GenerationTaskRepository(ABC):
    """GenerationTask 仓储抽象接口。"""

    @abstractmethod
    async def add(self, task: GenerationTask) -> None:
        """受理登记（INSERT 语义，主键冲突抛 DuplicateEntityError）。"""
        raise NotImplementedError

    @abstractmethod
    async def get_by_id(self, entity_id: str) -> GenerationTask | None:
        """按 entity_id 查询任务，不存在返回 None。"""
        raise NotImplementedError

    @abstractmethod
    async def save(
        self, task: GenerationTask, *, expected_version: int
    ) -> GenerationTask:
        """乐观并发更新（UPDATE ... WHERE entity_version = expected_version）。

        影响行数为 0（版本被并发写入抢先）抛 CONCURRENT_MODIFICATION。
        返回更新后的聚合（数据库回读的一致快照）。
        """
        raise NotImplementedError

    @abstractmethod
    async def claim_pending(
        self,
        *,
        actor: str,
        worker_id: str,
        lease_expires_at: datetime,
        now: datetime,
        limit: int,
    ) -> list[str]:
        """原子批量领取 PENDING 任务，返回**已归属调用者**的任务 id。

        为什么不是"先 list 再逐个 claim"：并发 worker 会读到同一个
        "最早 N 条"窗口，然后逐个用乐观锁抢——正确但浪费，N 个 worker 抢
        同一批 id，只有一个成功，其余全是空转（见
        docs/evidence/phase1-resilience.md 的争抢计数）。这里用一条
        ``UPDATE ... WHERE entity_id IN (SELECT ... FOR UPDATE SKIP LOCKED)``
        把"挑选"与"占有"合成一个原子动作：并发实例各自拿到不重叠的批次。

        状态合法性仍由数据库守卫兜底（PENDING→RUNNING 是登记边）。
        """
        raise NotImplementedError

    @abstractmethod
    async def list_expired_leases(
        self, limit: int, *, now: datetime
    ) -> list[GenerationTask]:
        """租约已过期的 RUNNING 任务（崩溃 worker 的遗留，9.4 崩溃恢复入口）。

        只返回 ``lease_expires_at`` 非空且已过期的行：租约为空的 RUNNING 行
        属于迁移前存量，迁移已回填窗口；没有租约事实的行不该被静默抢占。
        """
        raise NotImplementedError

    @abstractmethod
    async def take_over(
        self,
        task_id: str,
        *,
        worker_id: str,
        expires_at: datetime,
        now: datetime,
    ) -> bool:
        """原子接管过期租约（谓词 CAS：状态 RUNNING 且租约已过期）。

        返回 True = 本实例接管成功；False = 租约未到期/已被他人接管/任务已
        终态。谓词在数据库里与写入同一条 UPDATE 求值，多个 worker 同时看到
        同一批过期任务时只有一个能接管——这是"不会重复执行同一任务"的
        实际保证，不能退化成先读后写的 check-then-act。
        """
        raise NotImplementedError
