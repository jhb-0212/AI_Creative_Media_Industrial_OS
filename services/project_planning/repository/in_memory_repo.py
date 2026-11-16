"""Project 内存仓储实现（Phase 1 初期，无 DB 依赖）。

说明：作为 PostgreSQL 实现落地前的可替换实现，用于跑通主链与测试。
后续接入 SQLAlchemy 后，本实现可保留用于单测。

**本实现不 import 任何数据库驱动**：它抛的是领域异常（``DuplicateEntityError``
与 ``ConcurrentModificationError``），与 SQL 实现**翻译后**的语义一致。
此前它用 ``IntegrityError`` 同时模拟"重复插入"与"乐观锁失败"——而两者处置
方式相反（幂等返回 vs 基于新版本重试）。内存实现照抄这个含混，会让单测通过
而集成行为不同（审查第 16 条）。
"""

from __future__ import annotations

from packages.errors.repository_errors import DuplicateEntityError
from packages.shared.versioning import ConcurrentModificationError
from services.project_planning.domain.project import Project
from services.project_planning.repository.project_repo import ProjectRepository


class InMemoryProjectRepository(ProjectRepository):
    """基于 dict 的内存仓储。"""

    def __init__(self) -> None:
        self._store: dict[str, Project] = {}

    async def add(self, project: Project) -> None:
        """INSERT 语义：主键已存在 → ``DuplicateEntityError``（与 SQL 实现一致）。"""
        if project.entity_id in self._store:
            raise DuplicateEntityError(f"project_id={project.entity_id}")
        self._store[project.entity_id] = project

    async def save(self, project: Project, expected_version: int) -> None:
        """乐观锁语义：版本不匹配 → ``ConcurrentModificationError``。

        与 SQL 实现的 ``conditional_update``（命中 0 行即抛同一异常）对齐，
        而不是抛一个"看起来也像冲突"的驱动异常。
        """
        existing = self._store.get(project.entity_id)
        if existing is None or existing.entity_version != expected_version:
            raise ConcurrentModificationError(project.entity_id, expected_version)
        self._store[project.entity_id] = project

    async def get_by_id(self, entity_id: str) -> Project | None:
        return self._store.get(entity_id)
