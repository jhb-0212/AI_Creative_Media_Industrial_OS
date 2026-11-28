"""R16 持久层端到端验证脚本（在真实 Postgres 上跑服务→仓储→落库）。

用途：不依赖 HTTP，直接证明 production_routes / generation_tasks /
shot_budget_requests / budget_ledger / gate_decisions 五张表可经领域服务与
SQLAlchemy 仓储真实写入并可回读。运行前需可连库（环境变量指向目标库）：

    $env:POSTGRES_HOST='127.0.0.1'; $env:POSTGRES_PORT='15432'
    $env:POSTGRES_USER='postgres'; $env:POSTGRES_PASSWORD='postgres'
    $env:POSTGRES_DB='ai_film_industrial'
    python scripts/verify_r16_persistence.py

以超级用户连接（BYPASSRLS），只验证写入语义与 ORM 映射；RLS 策略的存在性
另由迁移断言覆盖。脚本结束会按本次验证的 tenant_id 清理写入的行（五张表都带
信封列 tenant_id，故一条统一条件即可覆盖）。
"""

from __future__ import annotations

import asyncio
import uuid

from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from packages.shared.database import build_migration_url
from packages.shared.outbox_writer import SqlAlchemyOutboxWriter
from packages.shared.unit_of_work import unit_of_work
from services.budget_quota.repository.orm import (
    BudgetLedgerModel,
    ShotBudgetRequestModel,
)
from services.budget_quota.repository.sqlalchemy_repos import (
    SqlAlchemyBudgetLedgerRepository,
    SqlAlchemyShotBudgetRequestRepository,
)
from services.budget_quota.service.budget_service import BudgetService
from services.production_planning.repository.orm import (
    GenerationTaskModel,
    ProductionRouteModel,
)
from services.production_planning.repository.sqlalchemy_repos import (
    SqlAlchemyGenerationTaskRepository,
    SqlAlchemyProductionRouteRepository,
)
from services.production_planning.service.generation_service import GenerationService
from services.production_planning.service.production_route_service import (
    ProductionRouteService,
)
from services.qc_governance.repository.orm import GateDecisionModel
from services.qc_governance.repository.sqlalchemy_repo import (
    SqlAlchemyGateDecisionRepository,
)
from services.qc_governance.service.gate_service import GateService

_TABLES = (
    ProductionRouteModel,
    GenerationTaskModel,
    ShotBudgetRequestModel,
    BudgetLedgerModel,
    GateDecisionModel,
)


async def _run() -> None:
    engine = create_async_engine(build_migration_url())
    maker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    tenant = uuid.uuid4()
    try:
        # 1. 生产路线（选择）
        async with unit_of_work(maker) as uow:
            route_repo = SqlAlchemyProductionRouteRepository(uow.session)
            writer = SqlAlchemyOutboxWriter(uow.session)
            route_svc = ProductionRouteService(route_repo, writer)
            route = await route_svc.select_route(
                project_id="proj-v",
                shot_id="shot-v",
                task_ref="task-v",
                route_type="IMAGE_TO_VIDEO",
                capability_ref="cap-v",
                basis_ref="basis-v",
                created_by="verifier",
                tenant_id=tenant,
            )
            await uow.commit()
        assert route.status.value == "VERIFIED"

        # 2. 生成任务（创建即 REQUESTED；执行结果只能由持租约的执行器提交）
        async with unit_of_work(maker) as uow:
            task_repo = SqlAlchemyGenerationTaskRepository(uow.session)
            writer = SqlAlchemyOutboxWriter(uow.session)
            task_svc = GenerationService(task_repo, writer)
            task = await task_svc.request_generation(
                project_id="proj-v",
                shot_id="shot-v",
                shot_revision_id="rev-v",
                production_route_ref=route.entity_id,
                model_adapter_ref="adapter-v",
                constraint_snapshot_id="snap-v",
                budget_ref="budget-v",
                prompt_ref="prompt-v",
                created_by="verifier",
                tenant_id=tenant,
            )
            await uow.commit()
        assert task.status.value == "REQUESTED"

        # 3. 预算申请 + 审批 + 账本追加
        async with unit_of_work(maker) as uow:
            req_repo = SqlAlchemyShotBudgetRequestRepository(uow.session)
            ledger_repo = SqlAlchemyBudgetLedgerRepository(uow.session)
            writer = SqlAlchemyOutboxWriter(uow.session)
            budget_svc = BudgetService(req_repo, ledger_repo, writer)
            request = await budget_svc.request_budget(
                project_id="proj-v",
                shot_id="shot-v",
                account_type="PRODUCTION",
                purpose="render",
                cost_category="render",
                amount_limit=1000,
                requested_by="requester",
                tenant_id=tenant,
            )
            await uow.commit()

        async with unit_of_work(maker) as uow:
            req_repo = SqlAlchemyShotBudgetRequestRepository(uow.session)
            ledger_repo = SqlAlchemyBudgetLedgerRepository(uow.session)
            writer = SqlAlchemyOutboxWriter(uow.session)
            budget_svc = BudgetService(req_repo, ledger_repo, writer)
            approved = await budget_svc.approve_budget(
                entity_id=request.entity_id,
                decision="APPROVED",
                approver="approver",
                decision_reason="",
                tenant_id=tenant,
            )
            await uow.commit()
        assert approved.status.value == "APPROVED"

        # 4. Gate 决策（追加式不可变记录 + 同事务事件）
        async with unit_of_work(maker) as uow:
            gate_repo = SqlAlchemyGateDecisionRepository(uow.session)
            writer = SqlAlchemyOutboxWriter(uow.session)
            gate_svc = GateService(gate_repo, writer)
            decision = await gate_svc.approve_gate(
                gate_key="GATE_V",
                project_id="proj-v",
                decision="APPROVED",
                decided_by="gate-owner-v",
                tenant_id=tenant,
                reason_code="PASS",
                evidence_refs=["ev-v"],
                reviewers=["rev-v"],
            )
            await uow.commit()

        # 5. 回读校验
        async with unit_of_work(maker, read_only=True) as uow:
            route_back = await SqlAlchemyProductionRouteRepository(uow.session).get_by_id(
                route.entity_id
            )
            task_back = await SqlAlchemyGenerationTaskRepository(uow.session).get_by_id(
                task.entity_id
            )
            req_back = await SqlAlchemyShotBudgetRequestRepository(uow.session).get_by_id(
                request.entity_id
            )
            ledger_rows = (
                await uow.session.execute(
                    text(
                        "SELECT amount, transaction_kind, idempotency_key "
                        "FROM budget_quota.budget_ledger "
                        "WHERE approval_ref = :ref"
                    ),
                    {"ref": request.request_id},
                )
            ).all()
            gate_back = await SqlAlchemyGateDecisionRepository(uow.session).get_by_id(
                decision.entity_id
            )

        assert route_back is not None and route_back.entity_id == route.entity_id
        assert task_back is not None and task_back.status.value == "SUCCEEDED"
        assert req_back is not None and req_back.status.value == "APPROVED"
        assert (
            len(ledger_rows) == 1 and ledger_rows[0][0] == 1000 and ledger_rows[0][1] == "RESERVE"
        )
        assert ledger_rows[0][2] == f"shot-budget:{request.request_id}"
        assert gate_back is not None and gate_back.decision == "APPROVED"
        assert gate_back.evidence_refs == ["ev-v"] and gate_back.reviewers == ["rev-v"]

        print(
            "[PASS] R16 持久层端到端：五表写入与回读一致（RESERVE 幂等键正确、"
            "Gate 决策 JSONB 字段往返无损）"
        )
    finally:
        # 清理本次写入的行（超级用户 DELETE）。
        async with maker() as session:
            for model in _TABLES:
                await session.execute(delete(model).where(model.tenant_id == tenant))
            await session.commit()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(_run())
