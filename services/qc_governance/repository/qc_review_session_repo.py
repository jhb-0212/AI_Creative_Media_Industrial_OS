"""QCReviewSession 仓储端口（有推进、无删除）。

为什么端口没有 ``delete``：会话是质量证据链的一环，Gate 与交付清单都可能引用
"某次复核得出的结论"。允许物理删除等于允许在交付后抹掉"当时怎么判的"。
数据库层同样不授 DELETE（守卫触发器直接 RAISE）。

为什么 ``update`` 必须带 ``expected_version``：状态推进会改 ``status`` 与
``decisions``，两个并发裁决基于同一旧版本各自写入就是 lost update——后写覆盖
先写，而两条结论都进了审计事件（审计链与状态不一致，比直接失败更糟）。
"""

from __future__ import annotations

from typing import Protocol

from services.qc_governance.domain.qc_review_session import QCReviewSession


class QCReviewSessionRepository(Protocol):
    """QC 复查会话的持久化端口。"""

    async def add(self, session: QCReviewSession) -> None:
        """追加一条会话（主键或"一报告一会话"唯一键冲突抛领域异常）。"""
        ...

    async def get_by_id(self, session_id: str) -> QCReviewSession | None:
        """按 entity_id 读取（受 RLS 约束：不可见即 None）。"""
        ...

    async def get_by_report(
        self, *, project_id: str, qc_report_ref: str
    ) -> QCReviewSession | None:
        """按"项目 + 报告号"查找既有会话（幂等回查与并发冲突后复查用）。"""
        ...

    async def update(
        self, session: QCReviewSession, *, expected_version: int
    ) -> None:
        """以乐观锁写回可变列（status / decisions / reviewers / updated_at）。

        只写这几列：``findings`` 等输入列由数据库守卫触发器禁止改写，
        整列回写会让每次推进都"重新声明"复查的输入是什么。
        """
        ...


__all__ = ["QCReviewSessionRepository"]
