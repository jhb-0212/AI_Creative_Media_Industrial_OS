"""QC 复查 API 路由（薄层，只做协议适配与 DTO 转换）。

契约对齐（actions / openapi）：
- ``QC.REVIEW.OPEN``   → ``POST /qc-review-sessions``（MEDIUM）
- ``QC.REVIEW.DECIDE`` → ``POST /qc-review-sessions/{session_id}/decisions``（HIGH）

三条边界：
- **身份不从请求体来**：``decided_by`` / ``created_by`` 一律取自可信 Principal，
  与 GateDecision 同口径（客户端能填就是身份伪造）。
- **不暴露读端点**：动作矩阵里没有 QC 复查的 READ 动作，未注册 Action 不得暴露
  端点（openapi 头部规则）。会话只能通过"开启/裁决"的响应看到，读能力随
  QC 工作台读接口一并设计（ADR-R36 §3 债务 4）。
- **不做 Gate 判定**：``GATE_ELIGIBLE`` 只代表"证据齐备、可以进 Gate"，
  Gate 结论只能由 ``PRODUCTION.GATE.APPROVE`` 产生。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.identity.authorization import ResourceRef
from packages.identity.enforcement import authorize
from packages.shared.contract_models import contract_model
from packages.shared.outbox_writer import SqlAlchemyOutboxWriter
from packages.shared.unit_of_work import UnitOfWork
from services.identity_access.deps import (
    Principal,
    get_qc_uow,
    require_action,
)
from services.qc_governance.domain.enums import (
    QCIssueHumanDecision,
    QCReviewSessionStatus,
)
from services.qc_governance.domain.ports import QCReportRefPort
from services.qc_governance.repository.sqlalchemy_qc_review_session_repo import (
    SqlAlchemyQCReviewSessionRepository,
)
from services.qc_governance.service.qc_review_service import QCReviewService

router = APIRouter(prefix="/qc-review-sessions", tags=["quality-governance"])

# 响应模型由契约定义生成：字段集永远等于 object_schemas 的 QCReviewSession。
_QCReviewSessionResponse = contract_model("QCReviewSession")


class OpenReviewSessionRequest(BaseModel):
    """开启复查会话（QC.REVIEW.OPEN）。

    ``findings`` 用 ``list[dict]`` 而不是逐字段模型：缺陷必填项由领域层
    ``QCIssue.from_mapping`` 校验（它同时保留检测器多给的字段）；在 API 层
    再写一份 Pydantic 模型会让必填项有两处定义，改一处忘一处就是漂移。
    """

    project_id: str = Field(..., min_length=1, max_length=64)
    asset_version_id: str = Field(..., min_length=1, max_length=64)
    qc_report_ref: str = Field(..., min_length=1, max_length=64)
    reference_set: dict[str, Any] = Field(default_factory=dict)
    reviewers: list[str] = Field(..., min_length=1)
    findings: list[dict[str, Any]] = Field(..., min_length=1)
    session_id: str | None = Field(default=None, max_length=64)


class ReviewDecisionItem(BaseModel):
    """一条缺陷的人工裁决（结论类型取自 21.1）。"""

    issue_id: str = Field(..., min_length=1, max_length=64)
    human_decision: QCIssueHumanDecision
    disposition: str = Field(..., min_length=1)
    waiver_authority: str | None = Field(default=None, max_length=128)
    waiver_reason: str | None = Field(default=None, max_length=512)


class DecideReviewRequest(BaseModel):
    """记录结论并推进会话（QC.REVIEW.DECIDE）。

    ``target_status`` 与当前状态相同时表示"只追加结论、不推进"；不等时必须是
    已登记的下一步（未登记 → 409 ``STATE_TRANSITION_DENIED``，含跳关）。
    """

    project_id: str = Field(..., min_length=1, max_length=64)
    target_status: QCReviewSessionStatus
    decisions: list[ReviewDecisionItem] = Field(default_factory=list)


def _get_report_ref_port(request: Request) -> QCReportRefPort:
    """组合根装配的 QC 报告引用端口（缺失即拒绝：不能"跳过校验先跑起来"）。"""
    port = getattr(request.app.state, "qc_report_ref_port", None)
    if port is None:
        raise DomainError(
            ErrorRegistry.INTERNAL_ERROR,
            detail="QC 报告引用端口未装配（app.state.qc_report_ref_port 缺失），拒绝开启会话",
        )
    return port  # type: ignore[no-any-return]


def _build_service(session: AsyncSession, port: QCReportRefPort) -> QCReviewService:
    """服务装配：RLS 会话 + 会话仓储 + 报告引用端口 + Outbox 写入端口。"""
    return QCReviewService(
        SqlAlchemyQCReviewSessionRepository(session),
        port,
        SqlAlchemyOutboxWriter(session),
    )


def _get_service(
    request: Request,
    uow: UnitOfWork = Depends(get_qc_uow),
    port: QCReportRefPort = Depends(_get_report_ref_port),
) -> QCReviewService:
    return _build_service(uow.session, port)


@router.post("", status_code=201, response_model=_QCReviewSessionResponse)
async def open_review_session(
    body: OpenReviewSessionRequest,
    principal: Principal = Depends(require_action("QC.REVIEW.OPEN")),
    uow: UnitOfWork = Depends(get_qc_uow),
    service: QCReviewService = Depends(_get_service),
) -> dict[str, Any]:
    """开启复查会话（同一报告幂等；缺陷必填项缺一即 422）。"""
    authorize(
        principal,
        "QC.REVIEW.OPEN",
        resource=ResourceRef(
            resource_type="QCReviewSession",
            organization_id=principal.organization_id,
            project_id=body.project_id,
        ),
    )
    session = await service.open_session(
        project_id=body.project_id,
        asset_version_id=body.asset_version_id,
        qc_report_ref=body.qc_report_ref,
        reference_set=body.reference_set,
        reviewers=body.reviewers,
        findings=body.findings,
        principal=principal,
        session_id=body.session_id,
    )
    await uow.commit()
    return session.to_dict()


@router.post(
    "/{session_id}/decisions", response_model=_QCReviewSessionResponse
)
async def decide_review(
    session_id: str,
    body: DecideReviewRequest,
    principal: Principal = Depends(require_action("QC.REVIEW.DECIDE")),
    uow: UnitOfWork = Depends(get_qc_uow),
    service: QCReviewService = Depends(_get_service),
) -> dict[str, Any]:
    """记录人工结论并推进会话（跳关一律 409；结论只追加不改写）。"""
    authorize(
        principal,
        "QC.REVIEW.DECIDE",
        resource=ResourceRef(
            resource_type="QCReviewSession",
            organization_id=principal.organization_id,
            project_id=body.project_id,
        ),
    )
    session = await service.decide(
        session_id=session_id,
        target_status=body.target_status,
        decisions=[item.model_dump() for item in body.decisions],
        principal=principal,
    )
    await uow.commit()
    return session.to_dict()
