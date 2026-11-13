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
    async def add(self, project: Project) -> None:
        """新增项目（INSERT 语义，主键冲突抛 IntegrityError）。

        与 save（upsert）区分：新增走本方法，使并发重复插入能被数据库
        唯一约束捕获，配合服务层幂等兜底（见 ProjectService.create_project）。
        """
        raise NotImplementedError

    @abstractmethod
    async def save(self, project: Project) -> None:
        """保存（新增或更新）项目。"""
        raise NotImplementedError

    @abstractmethod
    async def get_by_id(self, entity_id: str) -> Project | None:
        """按 entity_id 查询项目，不存在返回 None。"""
        raise NotImplementedError
