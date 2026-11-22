"""Project 仓储接口（抽象，数据所有权先行）。

设计要点：
- 只定义接口，不绑定具体存储（PostgreSQL/内存），实现可替换。
- 属于 project-planning 模块的专属数据写入边界（14.2 数据所有权矩阵）。
- 其他模块只能经服务/API 交互，不得直接访问本仓储。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from services.project_planning.domain.project import Project


class ProjectRepository(ABC):
    """Project 仓储抽象接口。"""

    @abstractmethod
    async def add(self, project: Project) -> None:
        """新增项目（INSERT 语义）。

        失败契约（端口声明，不由适配器的库决定）：
        - 主键/唯一冲突 → ``DuplicateEntityError``（可幂等回读）；
        - 外键/CHECK 不成立 → ``ReferentialIntegrityError``（不可幂等，上抛）。
        与 save（upsert）区分：新增走本方法，使并发重复插入能被数据库
        唯一约束捕获，配合服务层幂等兜底（见 ProjectService.create_project）。
        """
        raise NotImplementedError

    @abstractmethod
    async def save(self, project: Project, expected_version: int) -> None:
        """以乐观锁保存项目（语义同 ScriptRepository.save，见其文档）。"""
        raise NotImplementedError

    @abstractmethod
    async def get_by_id(self, entity_id: str) -> Project | None:
        """按 entity_id 查询项目，不存在返回 None。"""
        raise NotImplementedError

    @abstractmethod
    async def get_by_id_for_child_write(self, entity_id: str) -> Project | None:
        """读取项目并对该行加**共享锁**，供「在项目下创建集数」使用。

        为什么必须单独一个方法，而不是复用 ``get_by_id``
        ----------------------------------------------
        「项目已归档则不得再登记集数」这条检查，失效方式是**并发窗口**：
        无锁读到的 ``ACTIVE`` 在插入的那一刻可能已经被归档。数据库不会拒绝，
        因为它根本不知道这次插入与那次归档有语义冲突。这正是本项目在 SC-5
        上栽过的形态——「查的瞬间成立、写的瞬间不成立」。

        所以调用顺序必须是**先锁、再判、后写**，且三步在同一事务内。
        把锁做进一个名字带语义的方法里，是为了让「忘了加锁」变成编译期/协议级
        的缺失（抽象方法没实现就实例化不了），而不是运行期一个静默的窗口。

        为什么是共享锁（``FOR SHARE``）而不是 ``FOR KEY SHARE``
        ------------------------------------------------------
        ``FOR KEY SHARE`` 只与「修改键值 / 删除」的行锁冲突，**不与修改非键列的
        ``UPDATE`` 冲突**。而归档恰好是 ``UPDATE ... SET status = 'ARCHIVED'``，
        不改 ``entity_id``——用 ``FOR KEY SHARE`` 等于完全没加锁，并发窗口原样保留。

        ``FOR SHARE`` 与 ``FOR UPDATE`` 冲突，因此：归档方先拿到排他锁时，本方法
        阻塞至其提交，随后读到 ``ARCHIVED`` 并拒绝；本方先拿到共享锁时，归档方的
        ``UPDATE`` 阻塞至本方提交，于是两者被强制串行化为「先创建、后归档」——
        这在语义上是合法的（子对象在归档**之前**就已存在）。

        多个创建方之间是兼容的（共享锁互不阻塞），因此不会把同一项目下的并发
        创建串行化。
        """
        raise NotImplementedError
