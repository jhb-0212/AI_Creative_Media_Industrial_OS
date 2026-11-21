"""Scene 仓储接口（抽象，数据所有权先行）。

设计要点：
- 只定义接口，不绑定具体存储（PostgreSQL/内存），实现可替换。
- 属于 project-planning 模块的专属数据写入边界（14.2 数据所有权矩阵）。
  场次表虽然叫 ``scenes``，但它在 ``project_planning`` schema 内、由本模块
  独占写入——镜头域只能经 ``scene_id`` 引用它，不得直写。
- 其他模块只能经服务/API 交互，不得直接访问本仓储。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from services.project_planning.domain.scene import Scene


class SceneRepository(ABC):
    """Scene 仓储抽象接口。"""

    @abstractmethod
    async def add(self, scene: Scene) -> None:
        """新增场次（INSERT 语义）。

        失败契约见 ``ProjectRepository.add``：唯一冲突抛 ``DuplicateEntityError``
        （可幂等回读既有场次），外键/CHECK 不成立抛 ``ReferentialIntegrityError``
        （归属集或项目不可用，不可幂等）。
        """
        raise NotImplementedError

    @abstractmethod
    async def save(self, scene: Scene, expected_version: int) -> None:
        """以乐观锁保存场次（语义同 ScriptRepository.save，见其文档）。"""
        raise NotImplementedError

    @abstractmethod
    async def get_by_id(self, entity_id: str) -> Scene | None:
        """按 entity_id 查询场次，不存在返回 None。

        ``entity_id`` 与 ``scene_id`` 同值（数据库有 CHECK 兜底），故本方法同时
        就是「按场次标识查」——不再另开一个签名相同、语义重复的方法。
        """
        raise NotImplementedError

    @abstractmethod
    async def get_by_project_code(self, project_id: str, scene_code: str) -> Scene | None:
        """按项目 + 场次编号查询（幂等回读用）。

        编号是项目内唯一的展示键（``uq_scenes_project_code``）。无显式
        ``entity_id`` 的重复请求只能靠它找回既有行——若这里返回 None 而插入又
        撞了唯一约束，服务层会把可重试的冲突误判成内部错误。
        """
        raise NotImplementedError
