"""Episode 仓储接口（抽象，数据所有权先行）。

设计要点：
- 只定义接口，不绑定具体存储（PostgreSQL/内存），实现可替换。
- 属于 project-planning 模块的专属数据写入边界（14.2 数据所有权矩阵）。
- 其他模块只能经服务/API 交互，不得直接访问本仓储。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from services.project_planning.domain.episode import Episode


class EpisodeRepository(ABC):
    """Episode 仓储抽象接口。"""

    @abstractmethod
    async def add(self, episode: Episode) -> None:
        """新增集数（INSERT 语义）。

        失败契约见 ``ProjectRepository.add``：唯一冲突抛 ``DuplicateEntityError``
        （可幂等回读既有集数），外键/CHECK 不成立抛 ``ReferentialIntegrityError``
        （归属项目不可用，不可幂等）。
        """
        raise NotImplementedError

    @abstractmethod
    async def save(self, episode: Episode, expected_version: int) -> None:
        """以乐观锁保存集数（语义同 ScriptRepository.save，见其文档）。"""
        raise NotImplementedError

    @abstractmethod
    async def get_by_id(self, entity_id: str) -> Episode | None:
        """按 entity_id 查询集数，不存在返回 None。"""
        raise NotImplementedError

    @abstractmethod
    async def get_by_project_ref(self, project_id: str, episode_ref: str) -> Episode | None:
        """按项目 + 集数标识查询集数，不存在返回 None。

        这是「归属验证」的核心查询：确认某个 episode_ref 确实归属于
        指定 project，杜绝跨项目悬空引用。
        """
        raise NotImplementedError

    @abstractmethod
    async def get_by_project_ref_for_child_write(
        self, project_id: str, episode_ref: str
    ) -> Episode | None:
        """**带锁**按项目 + 集数标识读取集数（创建子对象前用）。

        与 ``get_by_project_ref`` 的区别只有一处：对目标行加 ``FOR SHARE``，
        使「读父状态 → 判定冻结 → 建子对象」与「归档父行」在并发下相遇。

        为什么需要它（实测，见 ``docs/audits/mig-recon/verify_parent_freeze_lock.py``）：
        - ``FOR KEY SHARE`` **挡不住** ``UPDATE ... SET status``（只与改键值/删除冲突），
          用它等于没加锁；
        - ``FOR SHARE`` 与归档的非键列更新冲突，且与子表 INSERT 的外键检查
          （取 KEY SHARE）不冲突，故不会误伤正常创建；
        - READ COMMITTED 下等锁结束后读到的是**新版本**（重检），不会拿着旧快照放行。

        调用方必须已处于事务中，否则锁在语句结束即释放、退化为无锁读
        （见 ``verify_parent_freeze_e2e.py`` 对会话事务边界的端到端断言）。

        刻意**不给默认实现**：有默认实现就等于允许某个适配器悄悄退回无锁读，
        而没有任何闸门会发现——与 ``transition_to`` 的 ``action`` 参数同理。
        """
        raise NotImplementedError
