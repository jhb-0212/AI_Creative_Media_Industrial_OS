"""ShotRevision 仓储接口（抽象，数据所有权先行）。

设计要点：
- 修订是 append-only 事实：新修订即新行，旧修订永远保留，绝不原地改写。
- 最新版本由 revision 号派生（get_active_by_shot 取 revision 最大者）。
- 属于 shot-planning 模块的专属数据写入边界（ADR-R27）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from services.shot_planning.domain.shot_revision import ShotRevision


class ShotRevisionRepository(ABC):
    """ShotRevision 仓储抽象接口。"""

    @abstractmethod
    async def add(self, revision: ShotRevision) -> None:
        """新增修订（INSERT 语义；同 shot 内 revision 唯一约束兜底并发）。"""
        raise NotImplementedError

    @abstractmethod
    async def get_by_id(self, entity_id: str) -> ShotRevision | None:
        """按 entity_id 查询修订，不存在返回 None。"""
        raise NotImplementedError

    @abstractmethod
    async def get_active_by_shot(self, shot_id: str) -> ShotRevision | None:
        """按 shot_id 查询最新修订（revision 最大者），不存在返回 None。

        这是「版本递增不覆盖」的核心查询：最新版本由 revision 号派生，
        不依赖状态标记（append-only 无状态转移）。
        """
        raise NotImplementedError
