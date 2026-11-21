"""Scene / Shot / ShotRevision / ShotBreakdownDraft / ReworkTask 仓储接口。

为什么五个接口放在一个文件：它们是**同一条事务链**上的五个参与者
（草案 → 确认 → Shot + ShotRevision；Shot 的返工守卫 → ReworkTask）。
分开成五个 30 行的模块不会让边界更清晰，只会让「谁和谁在同一事务里」
这件事在目录结构上消失。领域模型仍各自独立（domain/ 下一对象一文件）。

设计要点与 EpisodeRepository 一致：
- 只定义接口，不绑定存储；实现可替换（PostgreSQL / 内存）。
- 属于 project-planning 模块的专属写入边界（14.2 数据所有权）。
- ``add`` 的唯一冲突与引用失败分别映射为可幂等 / 不可幂等的端口错误。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from services.project_planning.domain.enums import ShotStatus
from services.project_planning.domain.rework_task import ReworkTask
from services.project_planning.domain.scene import Scene
from services.project_planning.domain.shot import Shot
from services.project_planning.domain.shot_draft import ShotBreakdownDraft
from services.project_planning.domain.shot_revision import ShotRevision


class SceneRepository(ABC):
    @abstractmethod
    async def add(self, scene: Scene) -> None:
        """新增场次（INSERT）。唯一冲突 → DuplicateEntityError；归属失败 → ReferentialIntegrityError。"""
        raise NotImplementedError

    @abstractmethod
    async def save(self, scene: Scene, expected_version: int) -> None:
        """乐观锁保存（条件更新，命中 0 行抛 ConcurrentModificationError）。"""
        raise NotImplementedError

    @abstractmethod
    async def get_by_id(self, entity_id: str) -> Scene | None:
        raise NotImplementedError

    @abstractmethod
    async def get_by_project_code(self, project_id: str, scene_code: str) -> Scene | None:
        """按项目 + 场次编号查询（幂等键与唯一约束口径一致：项目内唯一）。"""
        raise NotImplementedError

    @abstractmethod
    async def lock_for_archive(self, entity_id: str) -> Scene | None:
        """以**排他行锁**读取场次，锁持有到事务结束（归档前置条件的建立侧）。

        为什么这里的失败契约是「返回 None」而不是抛错：锁不到行有两种成因
        （不存在 / 不在当前作用域），调用方对两者给同一个 404——不泄漏存在性。
        真正的失败通道是「拿不到锁」，那会由数据库的等锁机制表达，不需要端口翻译。

        为什么必须由仓储提供而不是在服务层拼 SQL：锁模式（``FOR UPDATE``）与
        子对象侧的锁模式（``FOR KEY SHARE``）必须**成对**，任一侧写错都表现为
        「查了但没锁住」——静默失效。成对的声明放在同一层，才有机会被同一条用例
        两个方向各验一次。
        """
        raise NotImplementedError

    @abstractmethod
    async def lock_for_child_write(self, entity_id: str) -> Scene | None:
        """以**共享行锁**读取场次（子对象写入前的确认侧，与上一方法配对）。

        共享而非排他：子对象只需与「父行被排他改写的时刻」互斥，不应因此挡住
        父行的其它并发写（那会把所有镜头写路径串行化）。锁模式的选择理由见
        ``sqlalchemy`` 实现里的 ``_scene_lock_statement``。
        """
        raise NotImplementedError


class ShotRepository(ABC):
    @abstractmethod
    async def add(self, shot: Shot) -> None:
        raise NotImplementedError

    @abstractmethod
    async def save(self, shot: Shot, expected_version: int) -> None:
        raise NotImplementedError

    @abstractmethod
    async def get_by_id(self, entity_id: str) -> Shot | None:
        raise NotImplementedError

    @abstractmethod
    async def get_by_project_code(self, project_id: str, shot_code: str) -> Shot | None:
        """按项目 + 镜头编号查询（SH-3：shot_code 项目内唯一）。"""
        raise NotImplementedError

    @abstractmethod
    async def has_with_status(self, scene_id: str, status: ShotStatus) -> bool:
        """该场次下是否存在指定状态的镜头（SC-5 归档前置条件的唯一判定点）。

        放在仓储而不是服务里拼 SQL：判定口径（按 scene_id 过滤、按状态过滤）是
        数据访问细节，散在服务层会让「另一种写法」悄悄放宽守卫。
        """
        raise NotImplementedError


class ShotRevisionRepository(ABC):
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
    async def next_revision_no(self, shot_id: str) -> int:
        """下一个修订号（当前最大值 + 1；空集返回 1）。

        并发下两个请求可能拿到同一个号——(shot_id, revision_no) 唯一约束会拒绝
        第二个。**不在应用层加锁**：加锁只能保护单进程，唯一约束才是跨进程的。
        """
        raise NotImplementedError

    @abstractmethod
    async def get_active_by_shot(self, shot_id: str) -> ShotRevision | None:
        raise NotImplementedError


class ShotDraftRepository(ABC):
    @abstractmethod
    async def add(self, draft: ShotBreakdownDraft) -> None:
        raise NotImplementedError

    @abstractmethod
    async def save(self, draft: ShotBreakdownDraft, expected_version: int) -> None:
        raise NotImplementedError

    @abstractmethod
    async def get_by_id(self, entity_id: str) -> ShotBreakdownDraft | None:
        raise NotImplementedError


class ReworkTaskRepository(ABC):
    @abstractmethod
    async def add(self, task: ReworkTask) -> None:
        raise NotImplementedError

    @abstractmethod
    async def save(self, task: ReworkTask, expected_version: int) -> None:
        raise NotImplementedError

    @abstractmethod
    async def get_by_id(self, entity_id: str) -> ReworkTask | None:
        raise NotImplementedError
