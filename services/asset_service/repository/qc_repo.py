"""QC 报告仓储端口（append-only 证据记录）。"""

from __future__ import annotations

from abc import ABC, abstractmethod

from services.asset_service.domain.qc_report import QCReport


class QCReportRepository(ABC):
    """QCReport 持久化端口。"""

    @abstractmethod
    async def add(self, report: QCReport) -> None:
        """新增报告（不 commit）；同 id 重复由主键约束兜底。"""
        raise NotImplementedError

    @abstractmethod
    async def get_by_id(self, qc_report_id: str) -> QCReport | None:
        """按报告 id 读取（用于幂等返回）。"""
        raise NotImplementedError

    @abstractmethod
    async def count_by_asset(self, asset_version_id: str) -> int:
        """统计某资产版本下的报告数量（批准前必须有证据）。"""
        raise NotImplementedError
