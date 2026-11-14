"""QCReport 的 SQLAlchemy 仓储实现（append-only，无更新路径）。"""

from __future__ import annotations

from enum import Enum

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.errors.error_codes import DomainError, ErrorRegistry
from services.asset_service.domain.enums import DeviationClass, IntentMatchStatus
from services.asset_service.domain.qc_report import QCReport
from services.asset_service.repository.orm import QCReportModel
from services.asset_service.repository.qc_repo import QCReportRepository


def _optional_enum[E: Enum](enum_cls: type[E], value: str | None) -> E | None:
    """存量字符串 -> 枚举（非法值转结构化错误，不静默丢弃）。"""
    if value is None:
        return None
    try:
        return enum_cls(value)
    except ValueError as exc:
        raise DomainError(
            ErrorRegistry.INTERNAL_ERROR,
            detail=f"qc_reports 存量枚举值非法: {value!r}",
        ) from exc


def _to_domain(model: QCReportModel) -> QCReport:
    return QCReport(
        qc_report_id=model.qc_report_id,
        asset_version_id=model.asset_version_id,
        tenant_id=model.tenant_id,
        quality_profile_version=model.quality_profile_version,
        detector_versions=dict(model.detector_versions),
        findings=list(model.findings),
        created_by=model.created_by,
        benchmark_version=model.benchmark_version,
        deviation_class=_optional_enum(DeviationClass, model.deviation_class),
        intent_match_status=_optional_enum(
            IntentMatchStatus, model.intent_match_status
        ),
        human_decision=model.human_decision,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


def _to_orm(report: QCReport) -> QCReportModel:
    return QCReportModel(
        qc_report_id=report.qc_report_id,
        asset_version_id=report.asset_version_id,
        tenant_id=report.tenant_id,
        quality_profile_version=report.quality_profile_version,
        benchmark_version=report.benchmark_version,
        detector_versions=report.detector_versions,
        findings=report.findings,
        deviation_class=(
            report.deviation_class.value if report.deviation_class else None
        ),
        intent_match_status=(
            report.intent_match_status.value if report.intent_match_status else None
        ),
        human_decision=report.human_decision,
        created_by=report.created_by,
        created_at=report.created_at,
        updated_at=report.updated_at,
    )


class SqlAlchemyQCReportRepository(QCReportRepository):
    """基于 SQLAlchemy 异步会话的仓储实现。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, report: QCReport) -> None:
        async with self._session.begin_nested():
            self._session.add(_to_orm(report))

    async def get_by_id(self, qc_report_id: str) -> QCReport | None:
        stmt = select(QCReportModel).where(QCReportModel.qc_report_id == qc_report_id)
        result = await self._session.execute(stmt)
        model = result.scalar_one_or_none()
        return _to_domain(model) if model is not None else None

    async def count_by_asset(self, asset_version_id: str) -> int:
        stmt = (
            select(func.count())
            .select_from(QCReportModel)
            .where(QCReportModel.asset_version_id == asset_version_id)
        )
        result = await self._session.execute(stmt)
        return int(result.scalar_one())
