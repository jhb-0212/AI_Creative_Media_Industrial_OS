"""Episode 仓储接口（抽象，数据所有权先行）。

设计要点：
- 只定义接口，不绑定具体存储（PostgreSQL/内存），实现可替换。
- 属于 project-planning 模块的专属数据写入边界（14.2 数据所有权矩阵）。
- 其他模块只能经服务/API 交互，不得直接访问本仓储。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from services.project_planning.domain.episode import Episode


class EpisodeRepository(ABC):
    """Episode 仓储抽象接口。"""

    @abstractmethod
    async def add(self, episode: Episode) -> None:
        """新增集数（INSERT 语义，主键/唯一约束冲突抛 IntegrityError）。"""
        raise NotImplementedError

    @abstractmethod
    async def save(self, episode: Episode, expected_version: int) -> None:
        """以乐观锁保存集数（语义同 ScriptRepository.save，见其文档）。"""
        raise NotImplementedError

    @abstractmethod
    async def get_by_id(self, entity_id: str) -> Episode | None:
        """按 entity_id 查询集数，不存在返回 None。"""
        raise NotImplementedError

    @abstractmethod
    async def get_by_project_ref(
        self, project_id: str, episode_ref: str
    ) -> Episode | None:
        """按项目 + 集数标识查询集数，不存在返回 None。

        这是「归属验证」的核心查询：确认某个 episode_ref 确实归属于
        指定 project，杜绝跨项目悬空引用。
        """
        raise NotImplementedError
