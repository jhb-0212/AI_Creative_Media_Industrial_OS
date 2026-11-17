"""DeliveryManifest 仓储端口（依赖倒置：服务层不依赖 SQLAlchemy）。

刻意**没有通用的 `save`**：契约写「每个 Manifest 版本不可变」，内容列一旦写入
就没有任何更新入口。状态机（D4）通过单独的窄端口 `save_status` 推进：
它只更新 status/entity_version/updated_at/audit_ref 四列，且必须带期望版本做
乐观锁。比起提供通用 save 再约定"只准改 status"，窄端口让越界在类型层就不可表达。
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

    @abstractmethod
    async def save_status(
        self, manifest: DeliveryManifest, *, expected_version: int
    ) -> None:
        """只推进状态投影四列（status/entity_version/updated_at/audit_ref）。

        `expected_version` 做乐观锁：版本已被并发推进则抛
        `ConcurrentModificationError`，由用例层决定是否重放。内容列不在此端口
        的更新集合内，数据库列级授权与触发器再加两道兜底。
        """
        raise NotImplementedError


__all__ = ["DeliveryManifestRepository"]
