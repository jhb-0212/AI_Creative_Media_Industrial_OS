"""Project 内存仓储实现（Phase 1 初期，无 DB 依赖）。

说明：作为 PostgreSQL 实现落地前的可替换实现，用于跑通主链与测试。
后续接入 SQLAlchemy 后，本实现可保留用于单测。
"""

from __future__ import annotations

from services.project_planning.domain.project import Project
from services.project_planning.repository.project_repo import ProjectRepository


class InMemoryProjectRepository(ProjectRepository):
    """基于 dict 的内存仓储。"""

    def __init__(self) -> None:
        self._store: dict[str, Project] = {}

    async def save(self, project: Project) -> None:
        self._store[project.entity_id] = project

    async def get_by_id(self, entity_id: str) -> Project | None:
        return self._store.get(entity_id)
