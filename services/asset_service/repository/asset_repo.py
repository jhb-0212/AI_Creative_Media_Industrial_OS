"""资产版本仓储端口（领域服务依赖的抽象，依赖倒置）。

设计要点：
- 服务层不依赖 SQLAlchemy，便于用内存 fake 做纯单测（无数据库）。
  这条以前只是愿望——服务层曾 ``except IntegrityError``，即端口没有失败契约，
  只能借适配器的库异常推断失败。现在契约声明在本文件与 ``port_errors``（P2-03）。
- ``add`` 的失败契约：唯一冲突抛 ``DuplicateEntityError``（可幂等回读）；
  归属外键/CHECK 不成立抛 ``ReferentialIntegrityError``（不可幂等，必须上抛）。
- ``save`` 带 ``expected_version``：所有状态迁移必须走乐观锁，禁止无条件覆盖。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from services.asset_service.domain.asset_version import AssetVersion


class AssetRepository(ABC):
    """AssetVersion 持久化端口。"""

    @abstractmethod
    async def add(self, asset: AssetVersion) -> None:
        """新增一条版本记录（INSERT 语义，不 commit）。

        失败契约：``DuplicateEntityError``（同 id 已存在）/
        ``ReferentialIntegrityError``（project_id 归属不成立等）。
        """
        raise NotImplementedError

    @abstractmethod
    async def save(self, asset: AssetVersion, expected_version: int) -> None:
        """以乐观锁保存状态变更（不 commit，版本不匹配抛并发冲突）。"""
        raise NotImplementedError

    @abstractmethod
    async def get_by_id(self, entity_id: str) -> AssetVersion | None:
        """按主键读取；不存在返回 None（租户边界由 RLS 保证）。"""
        raise NotImplementedError
