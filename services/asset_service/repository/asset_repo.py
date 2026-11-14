"""资产版本仓储端口（领域服务依赖的抽象，依赖倒置）。

设计要点：
- 服务层不依赖 SQLAlchemy，便于用内存 fake 做纯单测（无数据库）。
- ``save`` 带 ``expected_version``：所有状态迁移必须走乐观锁，禁止无条件覆盖。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from services.asset_service.domain.asset_version import AssetVersion


class AssetRepository(ABC):
    """AssetVersion 持久化端口。"""

    @abstractmethod
    async def add(self, asset: AssetVersion) -> None:
        """新增一条版本记录（INSERT 语义，不 commit）。"""
        raise NotImplementedError

    @abstractmethod
    async def save(self, asset: AssetVersion, expected_version: int) -> None:
        """以乐观锁保存状态变更（不 commit，版本不匹配抛并发冲突）。"""
        raise NotImplementedError

    @abstractmethod
    async def get_by_id(self, entity_id: str) -> AssetVersion | None:
        """按主键读取；不存在返回 None（租户边界由 RLS 保证）。"""
        raise NotImplementedError
