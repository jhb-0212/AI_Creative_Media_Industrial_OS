"""generation-execution 域端口协议（依赖倒置：领域只依赖抽象）。

引用对象归属校验端口（``RefOwnershipPort``）：GENERATION.REQUEST 准入
校验需要确认 shot_spec_ref / visual_plan_ref / constraint_snapshot_ref
指向的对象**存在且属于请求声明的项目**。这些对象的所有者是 shot_planning
域——本域不 import shot_planning 的任何东西（R2：领域模块之间禁止直连），
只定义端口；适配器在组合根（apps/api）装配，复用 ADR-R28 的登记式解析器
（读永远经过所有者域自己的数据库身份）。

``RefOwnership`` 刻意只含 project_id 一个字段：归属校验只需要"在哪个项目"，
向消费域泄漏更多业务字段只会制造新的耦合面。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class RefOwnership:
    """引用对象的归属事实（最小暴露面：只有 project_id）。"""

    project_id: str


@runtime_checkable
class RefOwnershipPort(Protocol):
    """跨域引用对象归属解析端口（实现由组合根提供）。"""

    def can_resolve(self, resource_type: str) -> bool:
        """资源类型是否有解析器（未登记的类型 fail-closed 拒绝）。"""
        ...

    async def resolve(
        self,
        *,
        tenant_id: object,
        organization_id: object,
        resource_type: str,
        resource_id: str,
    ) -> RefOwnership | None:
        """解析引用对象归属；不存在返回 None（区分 404 与跨项目 422）。"""
        ...
