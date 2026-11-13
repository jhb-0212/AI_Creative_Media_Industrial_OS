"""ScriptRevision 仓储接口（抽象，数据所有权先行）。"""

from __future__ import annotations

from abc import ABC, abstractmethod

from services.project_planning.domain.script import ScriptRevision


class ScriptRepository(ABC):
    """ScriptRevision 仓储抽象接口。"""

    @abstractmethod
    async def add(self, script: ScriptRevision) -> None:
        """新增剧本版本（INSERT 语义，主键冲突抛 IntegrityError）。"""
        raise NotImplementedError

    @abstractmethod
    async def save(self, script: ScriptRevision) -> None:
        """保存（新增或更新）剧本版本。"""
        raise NotImplementedError

    @abstractmethod
    async def get_by_id(self, entity_id: str) -> ScriptRevision | None:
        """按 entity_id 查询剧本版本，不存在返回 None。"""
        raise NotImplementedError

    @abstractmethod
    async def get_latest_by_episode(
        self, project_id: str, episode_ref: str
    ) -> ScriptRevision | None:
        """查询指定项目 + 集数的最新未取代剧本版本（DRAFT/LOCKED 态）。

        版本链必须按「项目 + 集数」隔离：不同集各自维护独立版本链，
        避免导入 EP002 时误将 EP001 标记为 SUPERSEDED。
        """
        raise NotImplementedError
