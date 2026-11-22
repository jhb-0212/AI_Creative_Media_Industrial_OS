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

from services.project_planning.domain.constraint_pack import ConstraintPack
from services.project_planning.domain.enums import ShotStatus
from services.project_planning.domain.rework_task import ReworkTask
from services.project_planning.domain.scene import Scene
from services.project_planning.domain.shot import Shot
from services.project_planning.domain.shot_draft import ShotBreakdownDraft
from services.project_planning.domain.shot_revision import ShotRevision
from services.project_planning.domain.shot_spec import ShotSpec


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
        子对象侧的锁模式（``FOR SHARE``）必须**成对**，任一侧写错都表现为
        「查了但没锁住」——静默失效。成对的声明放在同一层，才有机会被同一条用例
        两个方向各验一次。
        """
        raise NotImplementedError

    @abstractmethod
    async def lock_for_child_write(self, entity_id: str) -> Scene | None:
        """以**共享行锁**读取场次（子对象写入前的确认侧，与上一方法配对）。

        共享而非排他：子对象只需要与「父行状态被改写的时刻」互斥，不需要独占
        父行——排他锁会让两个并发确认在同一场次上互相阻塞，而它们各自插入不同的
        镜头，并不冲突。

        为什么是 ``FOR SHARE`` 而不是 ``FOR KEY SHARE``：后者与 ``FOR NO KEY
        UPDATE`` 兼容，而 ``UPDATE ... SET status`` 取的正是 ``NO KEY UPDATE``，
        因此 KEY SHARE 挡不住一次没有显式排他锁的归档——屏障会静默失效，且与
        「两侧成对」的假设相反（它要求父侧必须写对）。``FOR SHARE`` 不依赖父侧。
        渲染结果与选择理由见 ``shot_planning_sqlalchemy_repos.scene_lock_statement``。
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

    @abstractmethod
    async def existing_ids_in_project(
        self, project_id: str, shot_ids: list[str]
    ) -> set[str]:
        """返回 ``shot_ids`` 中**确实属于该项目**的那些 id（A23）。

        为什么需要它：约束包的 ``shot_ids`` 是数组，数组元素无法用外键表达归属，
        库层只能靠触发器兜底（``trg_constraint_packs_shots`` 抛 23503）。但 23503
        会被统一翻译成「引用关系不成立，不可幂等」，调用方收到 409 时不知道该改
        哪个参数。服务层先查一次，能给出「这些镜头不在本项目」的 400。

        返回值是**命中集合**而不是布尔值：调用方要能点出具体是哪几个 id 越界，
        否则「有镜头不属于本项目」这句话在生产环境里等于没说。
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

    @abstractmethod
    async def lock_for_child_write(self, entity_id: str) -> ShotRevision | None:
        """以**共享行锁**读取修订（子对象写入前的确认侧，A22）。

        为什么子对象也要管这条：``ShotRevision.frozen_states`` 含 SUPERSEDED，
        而「修订被取代」是与「为它建规格」并发发生的另一件事。先查后写必然被
        穿透——查的瞬间修订还 ACTIVE，写之前另一个事务已把它推成 SUPERSEDED。
        共享锁与 ``UPDATE ... SET status``（取 ``NO KEY UPDATE``）冲突，因此
        取代侧不必额外改写加锁方式，屏障就成立。

        只与状态改写互斥、不与另一条建规格互斥，是刻意的：两个并发请求为同一
        修订建规格会各自插入，由 ``uq_shot_spec_per_revision`` 拒绝第二个——
        那把锁不该为「重复创建」付串行化代价。

        失败契约与 Scene 的同名方法一致：返回 None（不存在 / 不可见统一 404）。
        """
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


class ShotSpecRepository(ABC):
    """镜头规格仓储（A22）。

    只有四个方法，且 ``get_by_revision`` 不是「顺手加的查询」：``uq_shot_spec_per_revision``
    被违反时，服务层必须能回答**占用者是谁**。只说「冲突了」会让调用方无从判断
    是自己重放了同一个请求（回读即可）还是撞了别人的规格（要换修订）。
    """

    @abstractmethod
    async def add(self, spec: ShotSpec) -> None:
        """新增规格（INSERT）。唯一冲突 → DuplicateEntityError；归属失败 → ReferentialIntegrityError。"""
        raise NotImplementedError

    @abstractmethod
    async def save(self, spec: ShotSpec, expected_version: int) -> None:
        """乐观锁保存（条件更新，命中 0 行抛 ConcurrentModificationError）。"""
        raise NotImplementedError

    @abstractmethod
    async def get_by_id(self, entity_id: str) -> ShotSpec | None:
        raise NotImplementedError

    @abstractmethod
    async def get_by_revision(self, shot_revision_id: str) -> ShotSpec | None:
        """按绑定修订查询（口径与 uq_shot_spec_per_revision 一致：修订全局唯一）。"""
        raise NotImplementedError


class ConstraintPackRepository(ABC):
    """约束包仓储（A23）。

    只有五个方法，其中两个不是「顺手加的查询」：

    - ``get_active_by_pack``：部分唯一索引 ``uq_constraint_pack_single_active``
      被违反时，服务层必须能回答**生效中的是哪一版**。版本推进没有入口
      （``CONSTRAINT.PACK.STATUS.CHANGE`` 未实现）时，这条查询是唯一能把
      「新版本锁不上」翻译成可读原因的地方。
    - ``get_by_pack_version``：``uq_constraint_pack_version`` 冲突时点出占用者。

    刻意没有 ``update_content`` / ``delete``：包的锁定义务一旦生效，内容就不可
    原地改（7.9「变更须新建版本并 supersede 旧版本」）。多一个内容写入方法就多
    一条绕过版本语义的路径，而它不会有任何闸门会红。
    """

    @abstractmethod
    async def add(self, pack: ConstraintPack) -> None:
        """新增约束包（INSERT）。唯一冲突 → DuplicateEntityError；归属失败 → ReferentialIntegrityError。"""
        raise NotImplementedError

    @abstractmethod
    async def save(self, pack: ConstraintPack, expected_version: int) -> None:
        """乐观锁保存（条件更新，命中 0 行抛 ConcurrentModificationError）。"""
        raise NotImplementedError

    @abstractmethod
    async def get_by_id(self, entity_id: str) -> ConstraintPack | None:
        raise NotImplementedError

    @abstractmethod
    async def get_active_by_pack(self, constraint_pack_id: str) -> ConstraintPack | None:
        """该逻辑包当前 ACTIVE 的版本（同一 pack_id 至多一个，由部分唯一索引保证）。"""
        raise NotImplementedError

    @abstractmethod
    async def get_by_pack_version(
        self, constraint_pack_id: str, version: int
    ) -> ConstraintPack | None:
        """该逻辑包某个版本的实体（``uq_constraint_pack_version`` 冲突时点出占用者）。

        与 ``get_by_id`` 的区别不是「少一次扫描」：冲突发生在
        ``(constraint_pack_id, version)`` 上，占用者可能是**另一行**（不同
        entity_id）。只知道「版本重复」而不说「被哪一个实体占着」，调用方就无法
        判断该换 version 还是该换 pack_id。
        """
        raise NotImplementedError
