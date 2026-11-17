"""QCReviewSession 的 SQLAlchemy 仓储实现。

写路径只有两种：``add``（一次）与 ``update``（乐观锁、只写可变列）。没有
``delete``——与端口一致，数据库层也没有 DELETE 权限与路径。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.shared.persistence import translate_integrity_errors
from packages.shared.versioning import conditional_update
from services.qc_governance.domain.enums import QCReviewSessionStatus
from services.qc_governance.domain.qc_issue import QCIssue
from services.qc_governance.domain.qc_review_session import (
    QCReviewSession,
    ReviewDecision,
)
from services.qc_governance.repository.orm import QCReviewSessionModel
from services.qc_governance.repository.qc_review_session_repo import (
    QCReviewSessionRepository,
)


def _to_domain(model: QCReviewSessionModel) -> QCReviewSession:
    """库内行 → 领域对象（JSONB 里的缺陷重新走一遍必填项校验）。"""
    findings = [
        QCIssue.from_mapping(raw, index=index)
        for index, raw in enumerate(model.findings)
    ]
    decisions = [ReviewDecision.from_dict(raw) for raw in model.decisions]
    try:
        status = QCReviewSessionStatus(model.status)
    except ValueError as exc:
        raise DomainError(
            ErrorRegistry.INTERNAL_ERROR,
            detail=f"qc_review_sessions 存量状态非法: {model.status!r}",
        ) from exc
    session = QCReviewSession.restore(
        entity_id=model.entity_id,
        project_id=model.project_id,
        asset_version_id=model.asset_version_id,
        qc_report_ref=model.qc_report_ref,
        reviewers=list(model.reviewers),
        findings=findings,
        tenant_id=model.tenant_id,
        created_by=model.created_by,
        organization_id=model.organization_id,
        reference_set=dict(model.reference_set),
        decisions=decisions,
        status=status,
        schema_version=model.schema_version,
        entity_version=model.entity_version,
        owner_module=model.owner_module,
        created_at=model.created_at,
        updated_at=model.updated_at,
        audit_ref=model.audit_ref,
    )
    return session


def _decisions_payload(session: QCReviewSession) -> list[dict[str, Any]]:
    return [decision.to_dict() for decision in session.decisions]


class SqlAlchemyQCReviewSessionRepository(QCReviewSessionRepository):
    """基于 SQLAlchemy 异步会话的仓储实现。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, session: QCReviewSession) -> None:
        # 主键冲突与"一报告一会话"唯一键冲突都由数据库裁决；这里翻译成领域异常，
        # 服务层不需要知道驱动细节。
        with translate_integrity_errors(entity=f"session_id={session.entity_id}"):
            async with self._session.begin_nested():
                self._session.add(
                    QCReviewSessionModel(
                        entity_id=session.entity_id,
                        entity_type="QCReviewSession",
                        schema_version=session.schema_version,
                        entity_version=session.entity_version,
                        status=session.status.value,
                        owner_module=session.owner_module,
                        tenant_id=session.tenant_id,
                        organization_id=session.organization_id,
                        created_at=session.created_at,
                        created_by=session.created_by,
                        updated_at=session.updated_at,
                        audit_ref=session.audit_ref,
                        project_id=session.project_id,
                        asset_version_id=session.asset_version_id,
                        qc_report_ref=session.qc_report_ref,
                        reference_set=dict(session.reference_set),
                        reviewers=list(session.reviewers),
                        findings=[issue.to_dict() for issue in session.findings],
                        decisions=_decisions_payload(session),
                    )
                )

    async def get_by_id(self, session_id: str) -> QCReviewSession | None:
        stmt = select(QCReviewSessionModel).where(
            QCReviewSessionModel.entity_id == session_id
        )
        result = await self._session.execute(stmt)
        model = result.scalar_one_or_none()
        return _to_domain(model) if model is not None else None

    async def get_by_report(
        self, *, project_id: str, qc_report_ref: str
    ) -> QCReviewSession | None:
        stmt = select(QCReviewSessionModel).where(
            QCReviewSessionModel.project_id == project_id,
            QCReviewSessionModel.qc_report_ref == qc_report_ref,
        )
        result = await self._session.execute(stmt)
        model = result.scalar_one_or_none()
        return _to_domain(model) if model is not None else None

    async def update(
        self, session: QCReviewSession, *, expected_version: int
    ) -> None:
        """只写可变列（status/decisions/reviewers/updated_at），按版本 CAS。

        为什么不用 ORM 的 dirty tracking：把实例 merge 回会话会把 ``findings``
        等输入列一起写进 UPDATE 语句，而数据库守卫会因此拒绝——"内容列不可变"
        这条约束若靠"我们不会改它"来满足，就不是约束。
        """
        await conditional_update(
            self._session,
            QCReviewSessionModel,
            session.entity_id,
            expected_version,
            {
                "status": session.status.value,
                "decisions": _decisions_payload(session),
                "reviewers": list(session.reviewers),
                "updated_at": session.updated_at,
            },
        )


__all__ = ["SqlAlchemyQCReviewSessionRepository"]
