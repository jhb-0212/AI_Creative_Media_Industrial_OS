"""ShotSpec 仓储接口（抽象，数据所有权先行）。

设计要点：
- 规格内容列不可原地改写（C2：历史版本不可改写）；锁定是唯一允许的
  授权变更（save 仅用于锁定推进）。
- 属于 shot-planning 模块的专属数据写入边界（ADR-R27）。
- 其它模块只能经服务/API 交互，不得直接访问本仓储。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from services.shot_planning.domain.shot_spec import ShotSpec


class ShotSpecRepository(ABC):
    """ShotSpec 仓储抽象接口。"""

    @abstractmethod
    async def add(self, spec: ShotSpec) -> None:
        """新增规格（INSERT 语义，主键冲突抛 DuplicateEntityError）。"""
        raise NotImplementedError

    @abstractmethod
    async def save(self, spec: ShotSpec, expected_version: int) -> None:
        """以乐观锁保存规格（仅锁定推进；内容列随行回写由迁移守卫兜底）。"""
        raise NotImplementedError

    @abstractmethod
    async def get_by_id(self, entity_id: str) -> ShotSpec | None:
        """按 entity_id 查询规格，不存在返回 None。"""
        raise NotImplementedError
