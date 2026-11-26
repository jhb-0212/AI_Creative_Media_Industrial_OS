"""QC 待检任务 API 路由（薄层，Phase 3）。

路由只做三件事：校验入参、取 UoW、调服务；状态与租约判定在领域层。
租约相关端点共用一个 action_key ``QC.TASK.PROCESS``：它们表达同一语义
「推进一个已有 QC 任务」，拆成多个 action 会让同一条边出现多个准入凭据。

``POST /qc-tasks`` 是**受控人工恢复入口**（C7-b），不是第二套 QC 创建机制
---------------------------------------------------------------------
入队一共有两个入口：``GENERATION_SUCCEEDED`` → relay 直调 ``QCTaskService``
（自动，不经 HTTP），以及本端点（人工，补一次漏掉的入队）。两条路径共用同一条
创建代码与同一个唯一键，所以「同一生成任务至多一个有效 QC 任务」只有一处可被
破坏。

这个端点此前的问题不是「校验不严」，而是**契约违约**：
``action_registry.yaml`` 早在 ``QC.TASK.ENQUEUE`` 上写死「仅在生成终态且携带
产物引用时入队」「同一生成任务不得重复入队」，而实现把请求体里的
``project_id`` / ``quality_profile_version`` 直接当事实用——持有该动作的调用方
可以为不存在的资产、或用自报口径建待检项，与 C7（只消费登记完成事件）和
C4（口径创建时冻结）同时冲突。因此现在：

* 身份只取 Principal（``QC.TASK.RECOVER`` 专项席位，C7-1/C7-2）；
* 归属与生命周期只取数据库事实，请求体只提供选择键与理由（C7-3/C7-4）；
* 幂等复用同一个唯一键，并把它翻译成 409 而非 500（C7-5）；
* 来源/操作者/理由随 ``QC_TASK_QUEUED`` 载荷落库（C7-6）。

租约持有者取自 **Principal**，不取自请求体
------------------------------------------
``lease_owner`` 的唯一作用，是让 ``require_lease_owner`` 拒绝非持有者续租/判定。
只要这个字符串来自请求体，那条判定就等于「谁知道这个名字，谁就是持有者」——
身份是自称的，租约退化成一个公开的字符串比较，任何人都能接管别人的检测任务
并写出 PASSED 判定（判定本身是 QC 存在的意义）。所以三个租约端点没有
``worker_id``，并由 ``extra="forbid"`` 让旧调用方带上它时直接 422。
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.shared.evidence_ports import ReportLookup
from packages.shared.generation_ports import GenerationFactsLookup
from packages.shared.unit_of_work import UnitOfWork
from services.identity_access.deps import (
    Principal,
    authorize_on_parent,
    get_asset_tenant_uow,
    get_qc_governance_tenant_uow,
    get_read_production_planning_tenant_uow,
    require_role,
)
from services.project_planning.repository.sqlalchemy_outbox_writer import (
    SqlAlchemyOutboxWriter,
)
from services.qc_governance.repository.qc_task_repo import SqlAlchemyQCTaskRepository
from services.qc_governance.repository.task_projection_repo import (
    SqlAlchemyTaskProjectionRepository,
)
from services.qc_governance.service.qc_task_service import QCTaskService

router = APIRouter(prefix="/qc-tasks", tags=["qc-task"])

_LEASE_MIN_SECONDS = 30
_LEASE_MAX_SECONDS = 3600

# ``app.state`` 上跨域端口工厂的键。与 ``apps/api/wiring.py`` 同名常量保持一致；
# 这里写字面量而不是反向 import，是因为 services 不得依赖 apps（R1），
# 由 ``tests/unit/test_cross_domain_port_wiring.py`` 断言两侧一致。
_LOOKUP_FACTORY_STATE_KEY = "qc_report_lookup_factory"
_FACTS_FACTORY_STATE_KEY = "generation_facts_lookup_factory"


def _service(session: AsyncSession) -> QCTaskService:
    return QCTaskService(
        SqlAlchemyQCTaskRepository(session),
        SqlAlchemyOutboxWriter(session),
        # 投影必须与源任务同一 Session：分开 session 就等于异步双写。
        projection=SqlAlchemyTaskProjectionRepository(session),
    )


def _get_service(
    uow: UnitOfWork = Depends(get_qc_governance_tenant_uow),
) -> QCTaskService:
    return _service(uow.session)


def _get_lookup_factory(request: Request) -> Any:
    """取组合根装配的核验器工厂；未装配时返回 ``None``（由服务层 fail-closed）。

    为什么不在这里直接构造：报告属资产域，QC 域 import 资产域仓储会违反
    分层规则 R2。端口由本域声明、适配由资产域提供、装配由 ``apps/`` 完成——
    见 ``apps/api/wiring.py`` 的说明。未装配时不在此报 500，而是交给服务层
    抛 ``INTERNAL_ERROR``：拒绝理由应写在**判定发生的地方**，而不是散落在路由。
    """
    return getattr(request.app.state, _LOOKUP_FACTORY_STATE_KEY, None)


def _get_facts_factory(request: Request) -> Any:
    """取生成事实读取器的工厂（适配由生产域提供）。"""
    return getattr(request.app.state, _FACTS_FACTORY_STATE_KEY, None)


class RecoverQCTaskRequest(BaseModel):
    """人工恢复入队：只说「恢复哪一条」与「为什么」，不说「它是什么」。

    为什么只留三个字段
    -----------------
    原模型要求 ``project_id`` / ``asset_version_id`` / ``quality_profile_version``
    全部由调用方填，于是这三个「事实」实际是自报的——C3 与 C4 都在这里被绕过。
    现在 ``project_id`` 降级为**选择键**（``generation_tasks`` 的唯一键含项目，
    单给任务 ID 在库层面就不唯一），并且必须被数据库事实确认；产物引用与口径
    版本一律从库里读，请求体里再出现即为 422。

    ``extra="forbid"`` 是这里唯一的防呆手段：静默忽略 ``asset_version_id``
    会让人以为自己指定了产物引用，实际被丢弃——那比报错更危险。
    """

    model_config = ConfigDict(extra="forbid")

    generation_task_id: str = Field(..., min_length=1, max_length=64)
    project_id: str = Field(..., min_length=1, max_length=64)
    reason: str = Field(..., min_length=1, max_length=200)


class LeaseClaimRequest(BaseModel):
    """领取 QC 任务：持有者取自 Principal（不由请求体自称）。"""

    model_config = ConfigDict(extra="forbid")

    lease_seconds: int = Field(300, ge=_LEASE_MIN_SECONDS, le=_LEASE_MAX_SECONDS)


class LeaseRenewRequest(BaseModel):
    """心跳续租：持有者取自 Principal。"""

    model_config = ConfigDict(extra="forbid")

    # 必填，不设默认：默认 0 会让旧客户端「看起来还能用」，实际每次都撞 fencing，
    # 而错误信息会指向「租约丢失」——真相是它从未携带代次。
    lease_generation: int = Field(..., ge=0)
    lease_seconds: int = Field(300, ge=_LEASE_MIN_SECONDS, le=_LEASE_MAX_SECONDS)


class FinishRequest(BaseModel):
    """QC 判定（RUNNING → PASSED / REJECTED）：报告不可为空。"""

    model_config = ConfigDict(extra="forbid")

    result: Literal["PASSED", "REJECTED"]
    qc_report_id: str = Field(..., min_length=1, max_length=64)
    evidence_refs: list[str] = Field(default_factory=list)
    lease_generation: int = Field(..., ge=0)


@router.post("", status_code=201)
async def recover_qc_task(
    body: RecoverQCTaskRequest,
    principal: Principal = Depends(require_role("QC.TASK.RECOVER")),
    uow: UnitOfWork = Depends(get_qc_governance_tenant_uow),
    facts_uow: UnitOfWork = Depends(get_read_production_planning_tenant_uow),
    facts_factory: Any = Depends(_get_facts_factory),
    service: QCTaskService = Depends(_get_service),
) -> dict[str, Any]:
    """人工恢复入队：判据全部来自数据库事实，幂等由唯一键兜底。

    顺序是「读事实 → 授权 → 写」，不是「写 → 授权」：
    归属判定的对象只能是数据库里的 project_id，所以必须先读；而授权必须先于
    任何写入，否则越权请求会先在库里留下行、再靠回滚抹掉——回滚一旦因为
    别的原因没生效，就变成实打实的越权写入。

    为什么跨域读用只读 UoW：这条路只读不写（``qc_governance_role`` 对
    ``production_planning`` 本就零授权），用写连接会让「其实只读」这个事实
    无法在数据库层被验证。
    """
    if facts_factory is None:
        raise DomainError(
            ErrorRegistry.INTERNAL_ERROR,
            detail=(
                "人工恢复缺少生成事实读取器：判据必须来自数据库，"
                "未装配 generation_facts_lookup_factory 时拒绝入队（fail-closed）"
            ),
        )
    lookup: GenerationFactsLookup = facts_factory(facts_uow.session)
    facts = await lookup(
        body.generation_task_id, body.project_id, principal.tenant_id
    )
    if facts is None:
        raise DomainError(
            ErrorRegistry.NOT_FOUND,
            detail=(
                f"生成任务不存在或不可见: {body.generation_task_id}"
                f"（项目 {body.project_id}）"
            ),
        )
    # 授权对象是**事实行里的** project_id：请求体只用来选择，不参与归属判定。
    await authorize_on_parent(
        "QC.TASK.RECOVER",
        principal,
        facts_uow.session,
        parent_type="Project",
        parent_id=facts.project_id,
    )
    task = await service.recover(
        facts=facts,
        operator_id=principal.subject_id,
        reason=body.reason,
    )
    await uow.commit()
    return task.to_dict()


@router.post("/{entity_id}/lease")
async def claim_qc_lease(
    entity_id: str,
    body: LeaseClaimRequest,
    principal: Principal = Depends(require_role("QC.TASK.PROCESS")),
    uow: UnitOfWork = Depends(get_qc_governance_tenant_uow),
    service: QCTaskService = Depends(_get_service),
) -> dict[str, Any]:
    """领取 QC 任务：QUEUED → RUNNING，并落下租约。"""
    task = await service.claim(
        task_id=entity_id,
        worker_id=principal.subject_id,
        lease_seconds=body.lease_seconds,
    )
    await uow.commit()
    return task.to_dict()


@router.post("/{entity_id}/lease/renew")
async def renew_qc_lease(
    entity_id: str,
    body: LeaseRenewRequest,
    principal: Principal = Depends(require_role("QC.TASK.PROCESS")),
    uow: UnitOfWork = Depends(get_qc_governance_tenant_uow),
    service: QCTaskService = Depends(_get_service),
) -> dict[str, Any]:
    """心跳续租：非持有者被拒绝。"""
    task = await service.renew(
        task_id=entity_id,
        worker_id=principal.subject_id,
        expected_lease_generation=body.lease_generation,
        lease_seconds=body.lease_seconds,
    )
    await uow.commit()
    return task.to_dict()


@router.post("/{entity_id}/lease/release")
async def release_qc_lease(
    entity_id: str,
    principal: Principal = Depends(require_role("QC.TASK.PROCESS")),
    uow: UnitOfWork = Depends(get_qc_governance_tenant_uow),
    service: QCTaskService = Depends(_get_service),
) -> dict[str, Any]:
    """回收到期租约：RUNNING → QUEUED，任务重新可领取。"""
    task = await service.release_expired(task_id=entity_id)
    await uow.commit()
    return task.to_dict()


@router.post("/{entity_id}/finish")
async def finish_qc_task(
    entity_id: str,
    body: FinishRequest,
    principal: Principal = Depends(require_role("QC.TASK.PROCESS")),
    uow: UnitOfWork = Depends(get_qc_governance_tenant_uow),
    asset_uow: UnitOfWork = Depends(get_asset_tenant_uow),
    lookup_factory: Any = Depends(_get_lookup_factory),
) -> dict[str, Any]:
    """判定：只有租约持有者可推进终态，且必须有**可核验**的 QC 报告。

    「有报告 ID」不等于「有证据」：报告必须在资产域真实存在，且属于本任务
    引用的资产版本与同一租户。核验跨域读一次资产域，值得——否则任何持有
    ``QC.TASK.PROCESS`` 的调用方提交一串随便编的 ID 就能推成 PASSED。
    """
    report_lookup: ReportLookup | None = (
        lookup_factory(asset_uow.session) if lookup_factory is not None else None
    )
    service = QCTaskService(
        SqlAlchemyQCTaskRepository(uow.session),
        SqlAlchemyOutboxWriter(uow.session),
        report_lookup=report_lookup,
        projection=SqlAlchemyTaskProjectionRepository(uow.session),
    )
    task = await service.finish(
        task_id=entity_id,
        worker_id=principal.subject_id,
        expected_lease_generation=body.lease_generation,
        result=body.result,
        qc_report_id=body.qc_report_id,
        evidence_refs=body.evidence_refs,
    )
    await uow.commit()
    return task.to_dict()


__all__ = ["router"]
