"""ConstraintPack 仓储接口（抽象，数据所有权先行）。

设计要点：
- 版本不可变（7.9）：变更须新建版本并 supersede 旧版本，不得原地覆盖；
  save 仅用于状态推进（激活/失效/取代/撤销），不用于内容改写。
- 属于 shot-planning 模块的专属数据写入边界（ADR-R27）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from services.shot_planning.domain.constraint_pack import ConstraintPack


class ConstraintPackRepository(ABC):
    """ConstraintPack 仓储抽象接口。"""

    @abstractmethod
    async def add(self, pack: ConstraintPack) -> None:
        """新增约束包（INSERT 语义，主键冲突抛 DuplicateEntityError）。"""
        raise NotImplementedError

    @abstractmethod
    async def save(self, pack: ConstraintPack, expected_version: int) -> None:
        """以乐观锁保存约束包（仅状态推进）。"""
        raise NotImplementedError

    @abstractmethod
    async def get_by_id(self, entity_id: str) -> ConstraintPack | None:
        """按 entity_id 查询约束包，不存在返回 None。"""
        raise NotImplementedError
