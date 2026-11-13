"""Project 仓储接口（抽象，数据所有权先行）。

设计要点：
- 只定义接口，不绑定具体存储（PostgreSQL/内存），实现可替换。
- 属于 project-planning 模块的专属数据写入边界（14.2 数据所有权矩阵）。
- 其他模块只能经服务/API 交互，不得直接访问本仓储。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from services.project_planning.domain.project import Project


class ProjectRepository(ABC):
    """Project 仓储抽象接口。"""

    @abstractmethod
    async def save(self, project: Project) -> None:
        """保存（新增或更新）项目。"""
        raise NotImplementedError

    @abstractmethod
    async def get_by_id(self, entity_id: str) -> Project | None:
        """按 entity_id 查询项目，不存在返回 None。"""
        raise NotImplementedError
