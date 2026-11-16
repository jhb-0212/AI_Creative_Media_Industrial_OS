"""GateDecision 仓储端口（append-only：没有 save，也没有 delete）。

为什么端口刻意不给 ``save`` / ``update`` / ``delete``：
- 契约与 7.4 的审计要求是"当时批的是什么"必须永远可信。只要存在一条更新通道，
  这个性质就只剩"约定"——而约定在并发、补数据、修 bug 三种场景下都会失守。
- 缺少通道不等于忘记加：修正裁决的正确做法是**新建一条**（新裁决 + 新证据），
  这在数据上自动留下"改过什么"的完整轨迹。
- 这条约束在三个层次重复出现，是刻意的纵深：端口（此处）、数据库授权
  （只授 SELECT/INSERT）、触发器（兜住超级用户与迁移角色）。
"""

from __future__ import annotations

from typing import Protocol

from services.qc_governance.domain.gate_decision import GateDecision


class GateDecisionRepository(Protocol):
    """Gate 裁决的持久化端口。"""

    async def add(self, decision: GateDecision) -> None:
        """追加一条裁决（主键或幂等键冲突时抛领域异常）。"""
        ...

    async def get_by_id(self, entity_id: str) -> GateDecision | None:
        """按 entity_id 读取（受 RLS 约束：不可见即 None）。"""
        ...

    async def find_by_idempotency_key(
        self,
        *,
        gate_instance_id: str,
        constraint_snapshot_id: str,
        ruleset_version: int,
        decision: str,
    ) -> GateDecision | None:
        """按幂等键查找既有裁决（并发冲突后回查用）。

        服务层在 ``add`` 抛 ``DuplicateEntityError`` 后用它回查：如果查到的记录
        与本次内容一致，说明是重放（幂等返回）；不一致说明客户端在改写事实
        （必须拒绝，否则"重复提交"就成了修改已发生裁决的后门）。
        """
        ...

    async def list_by_instance(self, gate_instance_id: str) -> list[GateDecision]:
        """列出某 Gate 实例的全部裁决（按 decided_at 升序，形成完整轨迹）。

        不提供"当前是否通过"的便捷方法：那个结论依赖"当前约束快照是哪一版"，
        是领域派生而不是存储事实；放进仓储会让两处各算一次并逐渐漂移。
        """
        ...


__all__ = ["GateDecisionRepository"]
