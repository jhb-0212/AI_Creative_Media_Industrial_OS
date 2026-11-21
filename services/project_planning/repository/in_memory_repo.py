"""Project / Scene 内存仓储实现（Phase 1 初期，无 DB 依赖）。

说明：作为 PostgreSQL 实现落地前的可替换实现，用于跑通主链与单测。
它现在是**真正的可替换实现**：只依赖端口契约异常（``port_errors``），
不再为了迎合服务层去伪造 ``sqlalchemy.exc.IntegrityError``——整改前这个
纯内存实现竟然 import 了 SQLAlchemy，这正是端口缺失败契约的体证（P2-03）。
"""

from __future__ import annotations

from dataclasses import replace

from packages.shared.port_errors import DuplicateEntityError
from packages.shared.versioning import ConcurrentModificationError
from services.project_planning.domain.project import Project
from services.project_planning.domain.scene import Scene
from services.project_planning.repository.project_repo import ProjectRepository
from services.project_planning.repository.scene_repo import SceneRepository


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

    async def get_by_id_for_child_write(self, entity_id: str) -> Project | None:
        """内存实现无行锁概念，等同于 ``get_by_id``。

        这不是「忘了实现」：进程内的 dict 不存在跨事务并发窗口，锁在这里没有
        可表达的语义。方法仍必须存在，因为端口是抽象方法——**锁的缺失应当是
        显式的（读到这段注释），而不是靠某个实现悄悄不写**。
        并发正确性只能由 SQL 实现 + 真实数据库验证，见 ADR-R27。
        """
        return self._store.get(entity_id)


class InMemorySceneRepository(SceneRepository):
    """基于 dict 的内存仓储（无 DB 依赖，供单测与主链演练）。

    两个唯一性必须自己守，且**与数据库同语义**：
    - 主键 ``entity_id`` 重复 → ``DuplicateEntityError``；
    - ``(project_id, scene_code)`` 重复 → 同样抛 ``DuplicateEntityError``，
      因为它对应 ``uq_scenes_project_code``。只守主键的实现会让单测通过、
      真实库报唯一冲突，那是最难查的一类「环境差异」。

    仓储内保存的是领域对象的浅拷贝，不是调用方传进来的同一引用：状态推进会
    原地改 ``entity_version``，若共享引用，乐观锁会拿改后版本与改前期望值比较，
    造成真实库不会有的假冲突。
    """

    def __init__(self) -> None:
        self._store: dict[str, Scene] = {}

    async def add(self, scene: Scene) -> None:
        if scene.entity_id in self._store:
            raise DuplicateEntityError(
                scene.entity_id, constraint="pk_project_planning_scenes"
            )
        existing = await self.get_by_project_code(scene.project_id, scene.scene_code)
        if existing is not None:
            raise DuplicateEntityError(
                scene.entity_id, constraint="uq_scenes_project_code"
            )
        # 存快照而非同一对象引用：领域对象在 advance_status 里会原地改 version，
        # 若仓储持有的是同一引用，乐观锁就会拿「改后的版本」和调用方传来的
        # 「改前版本」比较，必然假冲突——真实数据库不会有这种别名问题。
        self._store[scene.entity_id] = replace(scene)

    async def save(self, scene: Scene, expected_version: int) -> None:
        existing = self._store.get(scene.entity_id)
        if existing is None or existing.entity_version != expected_version:
            raise ConcurrentModificationError(scene.entity_id, expected_version)
        self._store[scene.entity_id] = replace(scene)

    async def get_by_id(self, entity_id: str) -> Scene | None:
        stored = self._store.get(entity_id)
        return replace(stored) if stored is not None else None

    async def get_by_project_code(
        self, project_id: str, scene_code: str
    ) -> Scene | None:
        for scene in self._store.values():
            if scene.project_id == project_id and scene.scene_code == scene_code:
                return replace(scene)
        return None
