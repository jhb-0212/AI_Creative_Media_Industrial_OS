"""QC 治理端到端验收：HTTP 入队 → 发现身份 → 租户领取 → 资产域报告核验 → 终态。

为什么这条链路必须单独存在
--------------------------------------------------------------
``tests/integration/test_qc_task_loop_pg.py`` 已经证明了「发现 → 领取 → 执行 →
PASSED → 投影清除」，但它的任务是由服务层 ``enqueue`` 直接播种的、报告是测试
自己编的桩事实。这意味着两件事从未被证明：

1. **HTTP 入口真的能产生一条可被调度看见的待检任务**。人工恢复（``POST
   /qc-tasks``）依赖「生成任务事实 → 归属授权 → 入队」三段，任何一段错位都
   只会让队列里少一行，而巡检脚本看到的仍是「0 条待检」——静默的空。
2. **判定前核验走的是真实资产域报告**，不是同名桩。报告核验的失效方式是
   静默的：核验器没接上就 fail-closed（表现为每次都 500），接错成「放行」则
   表现为「看起来通过」。只有让报告真的由资产域端点写出、再让 Worker 在真实
   连接上核验，才能证伪这两种情况。

因此本文件刻意不复用上一条用例的桩：任务来自 HTTP，报告来自 HTTP，核验器来自
``workers.qc.__main__`` 的装配函数（与生产进程同一个构造点），只有「检测器」
是桩——因为语义级画质判定需要模型网关，不在本轮授权范围。桩只负责说出
「我检完了、报告 id 是这个」，其余全部是真链路。

覆盖的两条：
- 正向：HTTP 入队 → 调度发现 → Worker 领取 → 真实报告核验通过 → PASSED，
  且任务离开调度投影。
- 负向：持有租约但拿**别的资产**的报告终结 → 被拒，任务留在 RUNNING，
  投影仍在（证明核验不是「有 id 就放行」）。

已知缺口（本轮首次被证伪，已加**入口守卫**，待完整打通）
--------------------------------------------------------
本文件的两条正向/负向用例走的是**租户内项目**（``organization_id`` 为空），
可以端到端跑通。带组织的项目跑不通，原因链是纯数据面的：

1. ``project_planning.projects`` 的 RLS 除租户外还要求
   ``organization_id = current_setting('app.current_organization_id')``；
2. ``asset_service.asset_versions`` 的 RLS 经 ``EXISTS (projects)`` 间接受此约束；
3. ``asset_service.qc_reports`` 的 RLS 又经 ``EXISTS (asset_versions)`` 传递；
4. 而 Worker 进程没有 Principal，``workers/qc/__main__.py`` 的
   ``_make_report_lookup`` 只能注入租户，注不进组织。

结果曾经是：组织内项目的任务**能入队，然后永远领不完**——检测器读不到资产
（``InspectionBlockedError``，不是假 REJECTED），核验读不到报告
（fail-closed），租约反复过期回队，任务在队列里打转。

现在的处置是**入口守卫**：``POST /qc-tasks`` 只要请求带组织声明就直接
409 ``PHASE_NOT_ENABLED``，并说明成因。代价是「带组织声明的租户级项目也会
被拒」（过度拒绝），换来的是「注定失败的任务绝不进队列」。见本文件第三条
用例。完整打通需要组织随事实链路传递（任务列 + 生成事实端口 + 事件载荷 +
Worker 作用域），那是一次带契约修订的独立批次，不在测试里改口径吞掉。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.api.main import app
from packages.shared.database import (
    create_qc_governance_engine,
    create_qc_scheduler_engine,
    create_sessionmaker,
)
from packages.shared.storage_locator import build_locator
from services.production_planning.repository.orm import GenerationTaskModel
from tests.conftest import make_test_token
from workers.qc.__main__ import (
    _make_report_lookup,
    _make_scope,
    _make_tenant_scope,
)
from workers.qc.scheduler import MaintenanceTenantDiscovery, TenantScheduler
from workers.qc.worker import QCInspection, QCWorker

_QUALITY_PROFILE = "qp-e2e-1"
_SHA = "f" * 64
_LOCATOR = build_locator(_SHA, "png")

# 独立租户：开发库里同一租户下还躺着历史用例遗留的 QUEUED 任务，若复用
# ``TEST_TENANT_ID``，调度发现会把它们一起交给 Worker，于是「PASSED」可能
# 落在别人的任务上——用例会绿，但证明的是另一件事。
_TENANT = uuid.uuid4()


def _actor(subject_id: str, roles: list[str]) -> AsyncClient:
    """真实 JWT + 真实 ASGI 端点；角色取自契约 ``allowed_principals`` 原文。

    刻意**不带组织声明**：本用例走的是租户内项目（``organization_id`` 为
    空）。原因不是省事，而是 Worker 侧报告核验的组织面缺口（见文件尾
    ``KNOWN_GAP``）——那是一条需要单独裁决的治理问题，不能靠本用例
    「顺手带个 org claim」掩盖成已支持。
    """
    token = make_test_token(
        subject_id=subject_id, roles=roles, tenant_id=_TENANT
    )
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    )


async def _create_project(client: AsyncClient) -> str:
    resp = await client.post(
        "/projects",
        json={
            "project_name": f"e2e_{uuid.uuid4().hex[:8]}",
            "project_tier": "B",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["entity_id"]


async def _create_asset(client: AsyncClient, project_id: str) -> str:
    resp = await client.post(
        "/assets",
        json={
            "project_id": project_id,
            "asset_type": "IMAGE",
            "sha256": _SHA,
            "storage_ref": _LOCATOR,
            "rights_manifest_ref": "rights-manifest-e2e",
            "entity_id": f"asset_{uuid.uuid4().hex[:10]}",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["asset_version_id"]


async def _create_report(
    client: AsyncClient, asset_version_id: str, report_id: str
) -> str:
    resp = await client.post(
        f"/assets/{asset_version_id}/qc-reports",
        json={
            "quality_profile_version": _QUALITY_PROFILE,
            "detector_versions": {"integrity": "e2e-1"},
            "findings": [{"code": "INTEGRITY", "verdict": "PASS"}],
            "qc_report_id": report_id,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["qc_report"]["qc_report_id"]


async def _seed_generation_task(
    admin_sm: async_sessionmaker[AsyncSession],
    *,
    project_id: str,
    asset_version_id: str,
) -> str:
    """播一条**成功终态**的生成任务事实（人工恢复入队的前提）。

    走 ORM 而不是拼 SQL：本用例造的是生产域事实行，字段会随模型演进；
    字符串 SQL 在这里会静默漂移，而它恰好是「入队判据是否成立」的输入。
    不存在的入队路径（``GENERATION_SUCCEEDED → relay``）由生成侧负责，
    本轮不复刻——这里只要一条「事实成立」的行。
    """
    now = datetime.now(UTC)
    task_id = f"gen_{uuid.uuid4().hex[:10]}"
    async with admin_sm() as session:
        session.add(
            GenerationTaskModel(
                entity_id=task_id,
                entity_type="GenerationTask",
                schema_version=1,
                entity_version=1,
                status="SUCCEEDED",
                owner_module="production_planning",
                tenant_id=_TENANT,
                created_at=now,
                created_by="e2e",
                updated_at=now,
                generation_task_id=task_id,
                project_id=project_id,
                shot_id=f"shot_{uuid.uuid4().hex[:8]}",
                shot_revision_id=f"rev_{uuid.uuid4().hex[:8]}",
                production_route_ref="route-e2e",
                model_adapter_ref="adapter-e2e",
                constraint_snapshot_id="csp-e2e",
                budget_ref="budget-e2e",
                prompt_ref="prompt-e2e",
                asset_version_id=asset_version_id,
                quality_profile_version=_QUALITY_PROFILE,
            )
        )
        await session.commit()
    return task_id


async def _recover(
    client: AsyncClient, generation_task_id: str, project_id: str, reason: str
) -> str:
    resp = await client.post(
        "/qc-tasks",
        json={
            "generation_task_id": generation_task_id,
            "project_id": project_id,
            "reason": reason,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["entity_id"]


async def _task_row(
    admin_sm: async_sessionmaker[AsyncSession], task_id: str
) -> Any:
    async with admin_sm() as session:
        return (
            await session.execute(
                text(
                    "SELECT status, lease_owner, lease_generation "
                    "FROM qc_governance.qc_tasks WHERE entity_id = :id"
                ),
                {"id": task_id},
            )
        ).one()


async def _projection_count(
    admin_sm: async_sessionmaker[AsyncSession], task_id: str
) -> int:
    async with admin_sm() as session:
        return int(
            await session.scalar(
                text(
                    "SELECT count(*) FROM qc_maintenance.task_projection "
                    "WHERE task_id = :id"
                ),
                {"id": task_id},
            )
        )


class _ReportReplayingInspector:
    """只说「检完了、报告是这个 id」的检测器：语义判定不在本轮范围。"""

    configured = True

    def __init__(self, report_id: str) -> None:
        self._report_id = report_id

    async def inspect(self, task: Any) -> QCInspection:
        return QCInspection(result="PASSED", qc_report_id=self._report_id)


class _ScopedDiscovery:
    """真实发现函数 + 用例侧过滤：只在开发库里认本用例的租户。"""

    def __init__(self, *, engine: Any, tenants: set[uuid.UUID]) -> None:
        self._inner = MaintenanceTenantDiscovery(engine)
        self._tenants = tenants

    async def tenants(self) -> list[uuid.UUID] | None:
        found = await self._inner.tenants()
        if found is None:
            return None
        return [t for t in found if t in self._tenants]


@pytest.fixture(scope="module")
async def qc_engines() -> AsyncIterator[tuple[Any, Any]]:
    """QC 池 + 发现身份池（与生产进程同一对引擎，避免用超级用户伪造边界）。"""
    qc_engine = create_qc_governance_engine()
    scheduler_engine = create_qc_scheduler_engine()
    try:
        yield qc_engine, scheduler_engine
    finally:
        await scheduler_engine.dispose()
        await qc_engine.dispose()


async def test_http_recovery_task_is_picked_up_and_finished_with_real_report(
    admin_sessionmaker, asset_sessionmaker, qc_engines
) -> None:
    """正向：HTTP 入队 → 发现 → 领取 → 真实报告核验 → PASSED → 离开投影。"""
    async with (
        _actor("producer_e2e", ["制片"]) as producer,
        _actor("asset_worker_e2e", ["Worker"]) as asset_worker,
        _actor("qc_worker_e2e", ["QC Worker"]) as qc_worker,
        _actor("recover_e2e", ["运维恢复操作者"]) as recover,
    ):
        project_id = await _create_project(producer)
        asset_id = await _create_asset(asset_worker, project_id)
        generation_task_id = await _seed_generation_task(
            admin_sessionmaker, project_id=project_id, asset_version_id=asset_id
        )
        task_id = await _recover(
            recover, generation_task_id, project_id, "漏入队补一次"
        )
        report_id = await _create_report(
            qc_worker, asset_id, f"rep_e2e_{uuid.uuid4().hex[:8]}"
        )

        assert await _projection_count(admin_sessionmaker, task_id) == 1, (
            "入队必须同事务落一条调度投影，否则调度永远看不见这条待检任务"
        )

        qc_engine, scheduler_engine = qc_engines
        sm = create_sessionmaker(qc_engine)
        inspector = _ReportReplayingInspector(report_id)
        # 核验器来自进程入口的装配函数：与生产 Worker 同一个构造点。
        report_lookup = _make_report_lookup(asset_sessionmaker)
        worker = QCWorker(
            scope=_make_scope(sm, report_lookup),
            scope_for_tenant=_make_tenant_scope(sm, report_lookup),
            executor=inspector,
            worker_id=f"e2e-{uuid.uuid4().hex[:8]}",
            lease_seconds=120,
        )
        scheduler = TenantScheduler(
            discovery=_ScopedDiscovery(
                engine=scheduler_engine, tenants={_TENANT}
            ),
            worker=worker,
        )
        outcome = await scheduler.run_once()

        assert outcome.blocked is False, "发现身份必须可用，否则整轮静默为空"
        assert outcome.tenant_errors == 0, "本用例租户必须被处理完"
        assert outcome.busy is True, "领到任务却被报成空闲，等于丢活"

        row = await _task_row(admin_sessionmaker, task_id)
        assert row.status == "PASSED", "真实报告核验通过后必须落到 PASSED"
        assert not row.lease_owner, "终态必须撤销租约"
        assert row.lease_generation >= 1, "领取必须留下可审计的代次"
        assert await _projection_count(admin_sessionmaker, task_id) == 0, (
            "终态任务必须离开调度投影；留着它调度会永远看到「有待检」"
        )


async def test_org_scoped_recovery_is_refused_before_it_enters_the_queue(
    admin_sessionmaker
) -> None:
    """组织级请求必须在入口被拒：宁可 409，也不让注定失败的任务进队列。

    这条不是「顺手测个 4xx」。没有它，缺口的表现是「任务能入队、Worker 反复
    领取反复失败、永远不终结」——那是一类最贵的故障：监控上看队列非空、
    Worker 上全是日志、但没有任何终态可交接。守卫把故障前移到请求方。

    同时断言**没有副产物**：拒绝路径不得留下 qc_tasks 行或投影行。
    """
    org = uuid.uuid4()
    async with admin_sessionmaker() as session:
        await session.execute(
            text(
                """
                INSERT INTO project_planning.organizations
                    (organization_id, tenant_id, name, created_at, created_by, updated_at)
                VALUES (:org, :tid, :name, now(), 'tester', now())
                """
            ),
            {"org": org, "tid": _TENANT, "name": f"e2e-org-{org.hex[:6]}"},
        )
        await session.commit()

    def _org_actor(subject_id: str, roles: list[str]) -> AsyncClient:
        token = make_test_token(
            subject_id=subject_id,
            roles=roles,
            tenant_id=_TENANT,
            organization_id=org,
        )
        return AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": f"Bearer {token}"},
        )

    async with (
        _org_actor("producer_org", ["制片"]) as producer,
        _org_actor("asset_worker_org", ["Worker"]) as asset_worker,
        _org_actor("recover_org", ["运维恢复操作者"]) as recover,
    ):
        resp = await producer.post(
            "/projects",
            json={
                "project_name": f"org_{uuid.uuid4().hex[:8]}",
                "project_tier": "B",
                "organization_id": str(org),
            },
        )
        assert resp.status_code == 201, resp.text
        project_id = resp.json()["entity_id"]
        asset_id = await _create_asset(asset_worker, project_id)
        generation_task_id = await _seed_generation_task(
            admin_sessionmaker, project_id=project_id, asset_version_id=asset_id
        )

        refused = await recover.post(
            "/qc-tasks",
            json={
                "generation_task_id": generation_task_id,
                "project_id": project_id,
                "reason": "组织级项目恢复尝试",
            },
        )
        assert refused.status_code == 409, refused.text
        body = refused.json()
        assert body["error_code"] == "PHASE_NOT_ENABLED", refused.text
        assert "组织" in body["message"], "拒绝理由必须指向组织上下文缺失，而不是泛化拒绝"

        async with admin_sessionmaker() as session:
            tasks = int(
                await session.scalar(
                    text(
                        "SELECT count(*) FROM qc_governance.qc_tasks "
                        "WHERE generation_task_id = :g"
                    ),
                    {"g": generation_task_id},
                )
            )
            projections = int(
                await session.scalar(
                    text(
                        "SELECT count(*) FROM qc_maintenance.task_projection p "
                        "JOIN qc_governance.qc_tasks t ON t.entity_id = p.task_id "
                        "WHERE t.generation_task_id = :g"
                    ),
                    {"g": generation_task_id},
                )
            )
        assert tasks == 0, "被入口拒绝的请求不得留下待检任务"
        assert projections == 0, "被入口拒绝的请求不得留下调度投影"


async def test_foreign_asset_report_is_refused_and_task_stays_running(
    admin_sessionmaker
) -> None:
    """负向：持有租约，但报告属于**另一个资产** → 拒绝，任务留在 RUNNING。

    「有报告 id」不等于「有证据」。若这条用例通过，说明核验只查了报告存在、
    没查归属——那任何持有 ``QC.TASK.PROCESS`` 的调用方都能拿无关报告把任务
    推成 PASSED。
    """
    async with (
        _actor("producer_e2e2", ["制片"]) as producer,
        _actor("asset_worker_e2e2", ["Worker"]) as asset_worker,
        _actor("qc_worker_e2e2", ["QC Worker"]) as qc_worker,
        _actor("recover_e2e2", ["运维恢复操作者"]) as recover,
    ):
        project_id = await _create_project(producer)
        asset_a = await _create_asset(asset_worker, project_id)
        asset_b = await _create_asset(asset_worker, project_id)
        generation_task_id = await _seed_generation_task(
            admin_sessionmaker, project_id=project_id, asset_version_id=asset_a
        )
        task_id = await _recover(recover, generation_task_id, project_id, "重复入队检查")

        lease = await qc_worker.post(
            f"/qc-tasks/{task_id}/lease", json={"lease_seconds": 120}
        )
        assert lease.status_code == 200, lease.text
        generation = lease.json()["lease_generation"]

        foreign_report = await _create_report(
            qc_worker, asset_b, f"rep_foreign_{uuid.uuid4().hex[:8]}"
        )
        refused = await qc_worker.post(
            f"/qc-tasks/{task_id}/finish",
            json={
                "result": "PASSED",
                "qc_report_id": foreign_report,
                "lease_generation": generation,
            },
        )
        assert refused.status_code == 422, (
            f"报告归属不符必须按「非法状态转移」拒绝，而不是 200 或泛化错误：{refused.text}"
        )
        assert refused.json()["error_code"] == "VALIDATION_ERROR", refused.text

        row = await _task_row(admin_sessionmaker, task_id)
        assert row.status == "RUNNING", "被拒后必须留在 RUNNING，等真实判定"
        assert await _projection_count(admin_sessionmaker, task_id) == 1, (
            "没终结就不能离开投影，否则这条待检任务从此消失"
        )
