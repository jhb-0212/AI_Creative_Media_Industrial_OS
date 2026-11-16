"""Gate 决策 API 路由（薄层，只做协议适配与 DTO 转换）。

契约对齐（contracts/action_registry.yaml）：
- ``PRODUCTION.GATE.APPROVE`` → ``POST /gate-decisions``
  （CRITICAL，「对应 Gate 授权人类职责主体」，「强制角色分离、证据与审批记录」，
  负向用例 ``ACT-GATE-NEG-001``「Agent/非授权人审批拒绝」）

为什么一个端点承接四种结论：契约按"动作"登记（一个 APPROVE 动作），而
APPROVED / REJECTED / RETURNED 是同一动作的三种结论。开三个端点会让
"谁能拒绝"与"谁能通过"分别授权——拒绝权与通过权分家，正是审批链失守的
常见形态。结论由请求体的 ``decision`` 表达，权限由同一个动作键约束。

为什么**不**接受 ``decided_by`` / ``decided_at`` / ``status``：
- ``decided_by`` 来自可信 Principal（服务层从令牌取），客户端能填就是身份伪造；
- ``decided_at`` 由服务端在记录时固定，客户端能填就能声称"我在变更前就批了"；
- ``status`` 恒为 RECORDED（决策事实没有生命周期），开放它等于允许改写历史。

关于职责分离（``ACT-GATE-NEG-001`` 的落地方式，与资产审批不同）：
本动作**不**传 ``applicant_subject_id``。不是因为忘记，而是 Gate 链在契约里
没有"申请人"载体——没有 GATE.REQUEST 类动作，``GateDecision`` 也没有 applicant
字段。硬把请求体里的一个字符串当申请人，等于把"谁提请的"交给调用方声明，
那比不检查更糟（会产生假的审计事实）。实际生效的分离有三条，都是可自证或
不可伪造的：① Agent 不得决策（授权层按 CRITICAL + 令牌里的主体类型）；
② 领域层与数据库 CHECK 再次拒绝 Agent 作为决策人；③ ``decided_by`` 必须
出现在 ``reviewers`` 中（决策人即参与审批的人）。
真实缺口与解锁条件登记在 ADR-R23 与 ``tests/permission/test_authorization_wiring.py``
的显式豁免表里。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

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
from services.qc_governance.domain.enums import GateDecisionValue, GateKey
from services.qc_governance.repository.sqlalchemy_gate_repo import (
    SqlAlchemyGateDecisionRepository,
)
from services.qc_governance.service.gate_service import GateService

router = APIRouter(prefix="/gate-decisions", tags=["quality-governance"])

# 响应模型由契约定义生成：字段集永远等于 object_schemas 的 GateDecision。
_GateDecisionResponse = contract_model("GateDecision")


class CreateGateDecisionRequest(BaseModel):
    """Gate 裁决请求体（PRODUCTION.GATE.APPROVE）。

    ``scope`` 以对象接收：契约声明为 object 但未定义内部结构（7.4 只说明
    "项目/集/场景/镜头"）。这里不做键名白名单——自造一套结构会与将来的
    契约定义冲突；只要求非空，原样保存并可被检索。
    """

    project_id: str = Field(..., min_length=1, max_length=64)
    gate_key: GateKey
    gate_instance_id: str = Field(..., min_length=1, max_length=64)
    decision: GateDecisionValue
    constraint_snapshot_id: str = Field(..., min_length=1, max_length=128)
    ruleset_version: int = Field(..., ge=1)
    entry_criteria: str = Field(..., min_length=1)
    required_artifacts: str = Field(..., min_length=1)
    reviewers: list[str] = Field(..., min_length=1)
    reason_code: str = Field(..., min_length=1, max_length=64)
    comment: str = Field(..., min_length=1)
    evidence_refs: list[str] = Field(default_factory=list)
    scope: dict[str, Any] = Field(..., min_length=1)
    entity_id: str | None = Field(default=None, max_length=64)


def _build_service(session: AsyncSession) -> GateService:
    """服务装配：RLS 会话 + 裁决仓储端口 + Outbox 写入端口。"""
    return GateService(
        SqlAlchemyGateDecisionRepository(session),
        SqlAlchemyOutboxWriter(session),
    )


def _get_service(uow: UnitOfWork = Depends(get_qc_uow)) -> GateService:
    """写用例服务装配（提交点由路由显式控制）。"""
    return _build_service(uow.session)


@router.post("", status_code=201, response_model=_GateDecisionResponse)
async def create_gate_decision(
    body: CreateGateDecisionRequest,
    principal: Principal = Depends(require_action("PRODUCTION.GATE.APPROVE")),
    uow: UnitOfWork = Depends(get_qc_uow),
    service: GateService = Depends(_get_service),
) -> dict[str, Any]:
    """记录一条 Gate 裁决（append-only；Agent 与非授权主体在依赖阶段即被拒）。"""
    # 资源级授权（B2）：组织取调用主体，项目取请求体——越界项目必须在这里真判
    # 一次。Gate 决策是 CRITICAL，跨组织批一个不属于自己组织的项目是最高危形态。
    authorize(
        principal,
        "PRODUCTION.GATE.APPROVE",
        resource=ResourceRef(
            resource_type="GateDecision",
            organization_id=principal.organization_id,
            project_id=body.project_id,
        ),
    )
    decision = await service.decide(
        project_id=body.project_id,
        gate_key=body.gate_key,
        gate_instance_id=body.gate_instance_id,
        decision=body.decision,
        constraint_snapshot_id=body.constraint_snapshot_id,
        ruleset_version=body.ruleset_version,
        entry_criteria=body.entry_criteria,
        required_artifacts=body.required_artifacts,
        reviewers=body.reviewers,
        reason_code=body.reason_code,
        comment=body.comment,
        evidence_refs=body.evidence_refs,
        scope=body.scope,
        principal=principal,
        entity_id=body.entity_id,
    )
    await uow.commit()
    return decision.to_dict()
