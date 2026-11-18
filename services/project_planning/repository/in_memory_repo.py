"""Project 内存仓储实现（Phase 1 初期，无 DB 依赖）。

说明：作为 PostgreSQL 实现落地前的可替换实现，用于跑通主链与单测。
它现在是**真正的可替换实现**：只依赖端口契约异常（``port_errors``），
不再为了迎合服务层去伪造 ``sqlalchemy.exc.IntegrityError``——整改前这个
纯内存实现竟然 import 了 SQLAlchemy，这正是端口缺失败契约的体证（P2-03）。
"""

from __future__ import annotations

from packages.shared.port_errors import DuplicateEntityError
from packages.shared.versioning import ConcurrentModificationError
from services.project_planning.domain.project import Project
from services.project_planning.repository.project_repo import ProjectRepository


class InMemoryProjectRepository(ProjectRepository):
    """基于 dict 的内存仓储。"""

    def __init__(self) -> None:
        self._store: dict[str, Project] = {}

    async def add(self, project: Project) -> None:
        # INSERT 语义：主键已存在即唯一冲突（端口契约，与 SQL 实现同语义）。
        if project.entity_id in self._store:
            raise DuplicateEntityError(
                project.entity_id, constraint="pk_project_planning_projects"
            )
        self._store[project.entity_id] = project

    async def save(self, project: Project, expected_version: int) -> None:
        # 乐观锁：版本不匹配等价于「条件 UPDATE 命中 0 行」，抛并发冲突
        # （与 ``conditional_update`` 同一异常类型，供测试验证并发路径）。
        existing = self._store.get(project.entity_id)
        if existing is None or existing.entity_version != expected_version:
            raise ConcurrentModificationError(project.entity_id, expected_version)
        self._store[project.entity_id] = project

    async def get_by_id(self, entity_id: str) -> Project | None:
        return self._store.get(entity_id)
