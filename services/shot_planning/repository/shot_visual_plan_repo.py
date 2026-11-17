"""ShotVisualPlan 仓储接口（抽象，数据所有权先行）。

设计要点：
- 锁定字段逐字段授权（19.2），历史 revision 不可改写（C.7）；
- 属于 shot-planning 模块的专属数据写入边界（ADR-R27）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from services.shot_planning.domain.shot_visual_plan import ShotVisualPlan


class ShotVisualPlanRepository(ABC):
    """ShotVisualPlan 仓储抽象接口。"""

    @abstractmethod
    async def add(self, plan: ShotVisualPlan) -> None:
        """新增视觉方案（INSERT 语义，主键冲突抛 DuplicateEntityError）。"""
        raise NotImplementedError

    @abstractmethod
    async def save(self, plan: ShotVisualPlan, expected_version: int) -> None:
        """以乐观锁保存视觉方案（状态推进/逐字段锁定）。"""
        raise NotImplementedError

    @abstractmethod
    async def get_by_id(self, entity_id: str) -> ShotVisualPlan | None:
        """按 entity_id 查询视觉方案，不存在返回 None。"""
        raise NotImplementedError
