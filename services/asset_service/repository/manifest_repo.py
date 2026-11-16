"""DeliveryManifest 仓储端口（依赖倒置：服务层不依赖 SQLAlchemy）。

只有 `add` 与 `get_by_id` 两个操作，**没有 `save`**：契约写「每个 Manifest 版本
不可变」，因此"修改清单"在端口层面就不存在——比起提供一个 save 再要求调用方
别用，去掉它才是可执行的约束。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from services.asset_service.domain.delivery_manifest import DeliveryManifest


class DeliveryManifestRepository(ABC):
    """交付清单持久化端口。"""

    @abstractmethod
    async def add(self, manifest: DeliveryManifest) -> None:
        """新增一份清单（INSERT 语义，不 commit）。"""
        raise NotImplementedError

    @abstractmethod
    async def get_by_id(self, manifest_id: str) -> DeliveryManifest | None:
        """按主键读取；不存在返回 None（租户/项目边界由 RLS 保证）。"""
        raise NotImplementedError


__all__ = ["DeliveryManifestRepository"]
