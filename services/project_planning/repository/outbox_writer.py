"""Outbox 写入端口（领域服务依赖的抽象）。

设计要点（依赖倒置 + 消除重复）：
- 领域服务不直接依赖 SQLAlchemy session，而是依赖 OutboxWriter 端口，
  与仓储端口同构，保证服务层可测试（内存 fake 替换）、可演进。
- 写入语义：仅登记事件（add），不 commit，事务边界由请求层统一控制，
  保证「业务状态变更 + 审计事件」同一事务原子提交。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any
from uuid import UUID


class OutboxWriter(ABC):
    """审计事件写入端口。"""

    @abstractmethod
    def record(
        self,
        *,
        aggregate_type: str,
        aggregate_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> UUID:
        """登记一条不可变审计事件（不 commit）。

        返回事件 id，供调用方关联或测试断言。
        """
        raise NotImplementedError
