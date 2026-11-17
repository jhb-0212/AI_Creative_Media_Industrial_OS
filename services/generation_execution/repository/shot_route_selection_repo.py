"""ShotRouteSelection 仓储接口（抽象，数据所有权先行）。

属于 generation-execution 模块的专属数据边界（V2.0.5 14.2 / A7 矩阵）。

端口刻意只有两个方法：
- ``add``：登记生效路线（**唯一约束冲突如实上抛** DuplicateEntityError——服务层
  正是靠它把并发第二条路线翻译成结构化拒绝，端口若在这里做"忽略冲突"或
  "先查后插"，C4 就退化成应用层约定）；
- ``get_effective``：读某项目某镜头的生效路线（不存在返回 None）。

没有 update/delete：生效路线是受理事实，不存在合法改写路径。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from services.generation_execution.domain.shot_route_selection import (
    ShotRouteSelection,
)


class ShotRouteSelectionRepository(ABC):
    """ShotRouteSelection 仓储抽象接口。"""

    @abstractmethod
    async def add(self, selection: ShotRouteSelection) -> None:
        """登记生效路线（INSERT 语义；(tenant, project, shot) 冲突抛 DuplicateEntityError）。"""
        raise NotImplementedError

    @abstractmethod
    async def get_effective(
        self, *, project_id: str, shot_id: str
    ) -> ShotRouteSelection | None:
        """查询镜头的生效路线；该镜头尚未选路时返回 None。"""
        raise NotImplementedError
