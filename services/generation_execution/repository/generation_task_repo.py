"""GenerationTask 仓储接口（抽象，数据所有权先行）。

属于 generation-execution 模块的专属数据边界（V2.0.5 14.2 / A7 矩阵）：
GenerationTask 表只有本域角色 svc_generation_execution 可写。

``save`` 携带 expected_version 做乐观并发控制：worker 领取（PENDING→RUNNING）
是竞争点，两个 worker 同时领取同一任务时只有一个能成功，失败方收到
CONCURRENT_MODIFICATION（幂等跳过，9.4 Worker 崩溃恢复的幂等语义）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

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
    async def list_pending(self, limit: int) -> list[GenerationTask]:
        """按创建时间序领取候选（status = PENDING，worker 轮询入口）。"""
        raise NotImplementedError
