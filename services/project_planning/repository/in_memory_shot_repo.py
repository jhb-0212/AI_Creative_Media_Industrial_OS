"""镜头域内存仓储（测试替身，语义对齐真实库）。

为什么存快照而不是对象引用：``advance_status`` 会原地推 version，若仓储持有
同一引用，乐观锁必然假冲突——真实库不会有这种别名问题（Scene 仓储已有同型教训）。
"""

from __future__ import annotations

from copy import deepcopy
from uuid import UUID

from packages.shared.port_errors import DuplicateEntityError
from packages.shared.versioning import ConcurrentModificationError
from services.project_planning.domain.enums import ShotRevisionStatus
from services.project_planning.domain.shot import Shot
from services.project_planning.domain.shot_revision import ShotRevision
from services.project_planning.repository.shot_repo import (
    ReworkTaskVerifier,
    ShotRepository,
    ShotRevisionRepository,
)


class InMemoryShotRepository(ShotRepository):
    def __init__(self) -> None:
        self._store: dict[str, Shot] = {}

    async def add(self, shot: Shot) -> None:
        if shot.entity_id in self._store:
            raise DuplicateEntityError(shot.entity_id, constraint="pk_shot_planning_shots")
        if await self.get_by_project_code(shot.project_id, shot.shot_code):
            raise DuplicateEntityError(
                shot.entity_id, constraint="uq_shots_project_code"
            )
        self._store[shot.entity_id] = deepcopy(shot)

    async def save(self, shot: Shot, expected_version: int) -> None:
        existing = self._store.get(shot.entity_id)
        if existing is None or existing.entity_version != expected_version:
            raise ConcurrentModificationError(shot.entity_id, expected_version)
        self._store[shot.entity_id] = deepcopy(shot)

    async def get_by_id(self, entity_id: str) -> Shot | None:
        stored = self._store.get(entity_id)
        return deepcopy(stored) if stored is not None else None

    async def get_by_project_code(
        self, project_id: str, shot_code: str
    ) -> Shot | None:
        for shot in self._store.values():
            if shot.project_id == project_id and shot.shot_code == shot_code:
                return deepcopy(shot)
        return None


class InMemoryShotRevisionRepository(ShotRevisionRepository):
    def __init__(self) -> None:
        self._store: dict[str, ShotRevision] = {}

    async def add(self, revision: ShotRevision) -> None:
        if revision.entity_id in self._store:
            raise DuplicateEntityError(
                revision.entity_id, constraint="pk_shot_planning_shot_revisions"
            )
        if await self.get_by_revision_no(revision.shot_id, revision.revision_no):
            raise DuplicateEntityError(
                revision.entity_id, constraint="uq_shot_revision_shot_no"
            )
        self._store[revision.entity_id] = deepcopy(revision)

    async def save(self, revision: ShotRevision, expected_version: int) -> None:
        existing = self._store.get(revision.entity_id)
        if existing is None or existing.entity_version != expected_version:
            raise ConcurrentModificationError(revision.entity_id, expected_version)
        self._store[revision.entity_id] = deepcopy(revision)

    async def get_by_id(self, entity_id: str) -> ShotRevision | None:
        stored = self._store.get(entity_id)
        return deepcopy(stored) if stored is not None else None

    async def active_head(self, shot_id: str) -> ShotRevision | None:
        candidates = [
            revision
            for revision in self._store.values()
            if revision.shot_id == shot_id
            and revision.status is ShotRevisionStatus.ACTIVE
        ]
        if not candidates:
            return None
        return deepcopy(max(candidates, key=lambda revision: revision.revision_no))

    async def get_by_revision_no(
        self, shot_id: str, revision_no: int
    ) -> ShotRevision | None:
        for revision in self._store.values():
            if revision.shot_id == shot_id and revision.revision_no == revision_no:
                return deepcopy(revision)
        return None

    async def lock_for_child_write(self, entity_id: str) -> ShotRevision | None:
        """内存实现没有行锁：语义等价于「读到一行的当前版本」。

        真实库的共享锁在并发侧才有意义（与 ``UPDATE ... SET status`` 互斥）；
        内存替身是单线程的，加锁无法被观测，因此只保留读语义——不假装有锁。
        """
        return await self.get_by_id(entity_id)


class InMemoryReworkTaskVerifier(ReworkTaskVerifier):
    def __init__(self) -> None:
        self._tasks: set[tuple[str, str, str, UUID | None]] = set()

    def add(
        self,
        task_id: str,
        *,
        project_id: str,
        tenant_id: UUID,
        organization_id: UUID | None,
    ) -> None:
        self._tasks.add((task_id, project_id, str(tenant_id), organization_id))

    async def exists_for_shot(
        self,
        task_id: str,
        *,
        project_id: str,
        tenant_id: UUID,
        organization_id: UUID | None,
    ) -> bool:
        return (
            task_id,
            project_id,
            str(tenant_id),
            organization_id,
        ) in self._tasks
