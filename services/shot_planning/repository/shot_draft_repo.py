"""ShotDraft 仓储接口（抽象，数据所有权先行）。

设计要点：
- 只定义接口，不绑定具体存储，实现可替换。
- 属于 shot-planning 模块的专属数据写入边界（ADR-R27）。
- 其它模块只能经服务/API 交互，不得直接访问本仓储。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from services.shot_planning.domain.shot_draft import ShotDraft


class ShotDraftRepository(ABC):
    """ShotDraft 仓储抽象接口。"""

    @abstractmethod
    async def add(self, draft: ShotDraft) -> None:
        """新增草案（INSERT 语义，主键冲突抛 DuplicateEntityError）。"""
        raise NotImplementedError

    @abstractmethod
    async def save(self, draft: ShotDraft, expected_version: int) -> None:
        """以乐观锁保存草案（状态推进：确认/取代）。"""
        raise NotImplementedError

    @abstractmethod
    async def get_by_id(self, entity_id: str) -> ShotDraft | None:
        """按 entity_id 查询草案，不存在返回 None。"""
        raise NotImplementedError
