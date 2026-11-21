"""镜头域仓储端口（Shot / ShotRevision / 返工依据）。

端口刻意保持窄：只有确认、状态推进与版本冲突判定真正需要的读取/写入，
不提供通用查询或修改业务字段的方法——ShotRevision 是不可变版本，多一个
``update_content`` 就会多一条绕过 CONFIRM 的写入路径。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from uuid import UUID

from services.project_planning.domain.shot import Shot
from services.project_planning.domain.shot_revision import ShotRevision


class ShotRepository(ABC):
    """Shot 聚合根写入端口。"""

    @abstractmethod
    async def add(self, shot: Shot) -> None:
        """INSERT 语义；重复身份/项目内编号抛 DuplicateEntityError。"""
        raise NotImplementedError

    @abstractmethod
    async def save(self, shot: Shot, expected_version: int) -> None:
        """乐观锁保存；版本不匹配抛 ConcurrentModificationError。"""
        raise NotImplementedError

    @abstractmethod
    async def get_by_id(self, entity_id: str) -> Shot | None:
        raise NotImplementedError

    @abstractmethod
    async def get_by_project_code(self, project_id: str, shot_code: str) -> Shot | None:
        raise NotImplementedError


class ShotRevisionRepository(ABC):
    """ShotRevision 写入端口（只增不改内容，唯一可变字段是 status）。"""

    @abstractmethod
    async def add(self, revision: ShotRevision) -> None:
        raise NotImplementedError

    @abstractmethod
    async def save(self, revision: ShotRevision, expected_version: int) -> None:
        raise NotImplementedError

    @abstractmethod
    async def get_by_id(self, entity_id: str) -> ShotRevision | None:
        raise NotImplementedError

    @abstractmethod
    async def active_head(self, shot_id: str) -> ShotRevision | None:
        """同一 shot_id 下 revision_no 最大的 ACTIVE 修订。"""
        raise NotImplementedError

    @abstractmethod
    async def get_by_revision_no(
        self, shot_id: str, revision_no: int
    ) -> ShotRevision | None:
        """版本冲突检测：同镜头同 revision_no 至多一条。"""
        raise NotImplementedError


class ReworkTaskVerifier(ABC):
    """返工依据存在性端口（只读，不把 workflow 表写权限引入镜头域）。"""

    @abstractmethod
    async def exists_for_shot(
        self,
        task_id: str,
        *,
        project_id: str,
        tenant_id: UUID,
        organization_id: UUID | None,
    ) -> bool:
        """必须同项目、同租户、同组织；跨项目借用真实任务号同样拒绝。"""
        raise NotImplementedError
