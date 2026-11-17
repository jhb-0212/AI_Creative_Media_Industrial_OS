"""ProductionRoute 仓储接口（抽象，数据所有权先行）。

属于 generation-execution 模块的专属数据边界（V2.0.5 14.2 / A7 矩阵）：
- 路线目录是平台能力配置数据，条目登记（播种）与验证标定走运维路径；
- 本端口只暴露「查询 + 登记」，状态推进无 HTTP 入口（UNVERIFIED→VERIFIED
  的标定入口是适配器验证工作流，契约未给 action——不猜契约，不造入口）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from services.generation_execution.domain.production_route import ProductionRoute


class ProductionRouteRepository(ABC):
    """ProductionRoute 仓储抽象接口。"""

    @abstractmethod
    async def add(self, route: ProductionRoute) -> None:
        """登记路线条目（INSERT 语义，主键冲突抛 DuplicateEntityError）。"""
        raise NotImplementedError

    @abstractmethod
    async def get_by_id(self, entity_id: str) -> ProductionRoute | None:
        """按 entity_id 查询路线，不存在返回 None。"""
        raise NotImplementedError
