"""审批引擎的跨域只读端口（谁拥有这个资源？）。

为什么需要它：``WORKFLOW.START_APPROVAL`` 发起票据时必须证明目标资源
**确实存在且属于票据声明的项目**——否则任何持票人都能为不存在的资源、
或别的项目的资源发起审批，把整个审批链变成空转的橡皮图章（R4-2 的
跨项目绕过正是这个缺口的一半）。

但 workflow 域的数据库角色（``svc_workflow``）读不到 asset / shot_planning
的表（ADR-R16 角色隔离），而跨服务 import 对方仓储违反 R2（14.2 数据
所有权）。解法是经典的六边形端口：

- **本模块只定义端口协议**（workflow 需要什么），不含任何实现；
- **组合根（apps/api）提供适配器**：按资源类型路由到拥有该表的域，
  用该域自己的只读仓储 + 该域自己的数据库身份去查——读永远经过
  所有者，workflow 只是消费"归属结论"这个最小事实；
- 适配器只暴露 ``project_id`` 一个字段，不做任何业务数据泄漏。

失败语义（fail-closed）：
- 资源类型没有解析器 → 服务层拒绝发起（不盲发）；
- 资源不存在 → NOT_FOUND；
- 归属项目与票据声明不一致 → VALIDATION_ERROR（与 shot/asset 域
  同口径：归属不一致是请求校验失败，不是授权失败——授权层已经
  判过发起人对**票据项目**的权限，这里判的是**资源事实**）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ResourceOwnership:
    """资源归属结论（最小事实：只回项目，不回业务数据）。"""

    project_id: str


class ResourceOwnershipPort(Protocol):
    """跨域资源归属解析端口（组合根注入，workflow 域不认识任何实现）。"""

    def can_resolve(self, resource_type: str) -> bool:
        """该资源类型是否有归属解析器。

        先问再查，让服务层能对"无解析器的类型"给出明确的 fail-closed
        错误，而不是把"查不到"和"查不了"混成一个 NOT_FOUND。
        """
        ...

    async def resolve(
        self,
        *,
        tenant_id: object,
        organization_id: object,
        resource_type: str,
        resource_id: str,
    ) -> ResourceOwnership | None:
        """解析资源归属；资源在租户内不存在返回 ``None``。

        归属解析按**租户全域**进行（不收敛到发起人的项目范围）：只有
        看得到跨项目行，才能区分"不存在"与"在别的项目"两种拒绝原因。
        适配器只输出归属结论，不泄漏资源内容，因此不构成越权读取。
        """
        ...
