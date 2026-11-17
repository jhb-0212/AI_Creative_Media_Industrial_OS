"""Phase 1 主链端到端集成测试（真实 API + 真实数据库 + 真实 worker 执行环）。

链路口径直接对齐 ``contracts/phases/phase_map.yaml`` 的 Phase 1 core_production：

    项目创建 → 剧本导入 → AI 拆镜草案 → 人工确认 → ShotSpec/ShotVisualPlan
    → ConstraintPack（如适用）→ 单一路线生成 → 核心 QC → 人工 Gate
    → DeliveryManifest

每一步都走**真实入口**（HTTP + JWT + Capability + RLS + 域角色），不 mock
服务层；只有两个环节以服务层直接调用（见"诚实边界"1、2）。

证明的四件事（缺了任一条，主链就其实没通）：
1. **状态串联**：每一步的产物状态是下一步准入的前提，而不是各自为政；
2. **产出落地**：worker 生成成功必须落成资产版本（ADR-R33 收口 R32 §3
   债务 3），且任务终态 ``result_ref`` 指向该资产版本，而不是一个只有
   适配器认识的字符串；
3. **审计链完整**：契约登记的事件沿同一条链真实到达 outbox（按 aggregate
   逐个核对，不靠"事件总数看起来对"）；
4. **证据与角色闸门真的会拦**：DRAFT 约束包进不了生成受理、同一镜头的第二条
   路线被拒且非生效路线进不了生成受理、GENERATED 资产进不了交付清单、
   制片头衔生成不了交付清单——负向断言就插在正向路径的同一个测试里，证明
   闸门在链上生效，而不是只在别的文件里存在。

诚实边界（不允许伪装成"已实现"）：
1. ShotSpec / ShotVisualPlan 的**创建**是内部领域用例（契约没有冻结对应
   action，见 ``services/shot_planning/api/spec_routes.py`` 的说明），因此
   以服务层直接建立，不伪造 HTTP 入口；镜头规格锁定（SHOTSPEC.LOCK）属
   Gate1 工序，本链路不使用。
2. 生产路线目录条目（ProductionRoute）是**平台能力**数据，其 VERIFIED 标定
   入口属适配器验证工作流（契约未给 action），以仓储播种，与生产环境
   "运维/验证工作流写入"同口径。
3. 链止于交付清单 DRAFT：``DELIVERY.MANIFEST.APPROVE``（联合审批，AND 语义）
   与 ``RELEASE`` 尚未实现（见 delivery_routes 模块说明），因此不推进到
   VALIDATED/RELEASED。
4. PictureLock 对象未建模：清单的 ``picture_lock_ref`` 引用本链真实产出的
   Gate4 APPROVED 裁决 id，不编造一个不存在的锁对象 id。
"""

from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.api.main import app
from packages.identity.principal import Principal, SubjectType
from packages.shared.outbox_writer import SqlAlchemyOutboxWriter
from services.asset_service.domain.enums import AssetStatus
from services.asset_service.repository.sqlalchemy_asset_repo import (
    SqlAlchemyAssetRepository,
)
from services.generation_execution.domain.enums import (
    GenerationTaskStatus,
    ProductionRouteStatus,
)
from services.generation_execution.domain.production_route import ProductionRoute
from services.generation_execution.repository.generation_task_sqlalchemy_repo import (
    SqlAlchemyGenerationTaskRepository,
)
from services.generation_execution.repository.production_route_sqlalchemy_repo import (
    SqlAlchemyProductionRouteRepository,
)
from services.shot_planning.repository.shot_revision_sqlalchemy_repo import (
    SqlAlchemyShotRevisionRepository,
)
from services.shot_planning.repository.shot_spec_sqlalchemy_repo import (
    SqlAlchemyShotSpecRepository,
)
from services.shot_planning.repository.shot_visual_plan_sqlalchemy_repo import (
    SqlAlchemyShotVisualPlanRepository,
)
from services.shot_planning.service.spec_service import ShotSpecService
from services.shot_planning.service.visual_plan_service import ShotVisualPlanService
from tests.conftest import TEST_TENANT_ID, apply_db_context, make_test_headers
from workers.generation.adapter import DeterministicGenerationAdapter
from workers.generation.asset_registrar import AssetVersionRegistrar
from workers.generation.executor import (
    GenerationExecutorContext,
    GenerationRoundStats,
    execute_round,
)

pytestmark = pytest.mark.asyncio

_TENANT = TEST_TENANT_ID


# --------------------------------------------------------------------------- #
# 夹具与辅助
# --------------------------------------------------------------------------- #
def _client(
    subject: str, roles: list[str], *, subject_type: str = "human"
) -> AsyncClient:
    """按角色构造真实鉴权客户端（访问令牌 + Capability 双凭证）。"""
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers=make_test_headers(
            subject_id=subject, roles=roles, subject_type=subject_type
        ),
    )


def _service_principal(subject: str = "director_001") -> Principal:
    """服务层直调用例的可信主体（模拟验签后的 Principal，不含请求体数据）。"""
    return Principal(
        subject_id=subject,
        subject_type=SubjectType.HUMAN,
        tenant_id=_TENANT,
        organization_id=_TENANT,
        roles=frozenset({"导演"}),
        project_ids=frozenset({"*"}),
    )


async def _seed_verified_route(
    gen_sm: async_sessionmaker[AsyncSession], *, project_scope: str
) -> str:
    """播种一条 VERIFIED 生产路线（平台能力目录，见诚实边界 2）。"""
    route = ProductionRoute(
        entity_id=f"route_{uuid.uuid4().hex[:8]}",
        route_type="blender_scripted",
        supported_control_signals=["camera_path", "blocking"],
        required_appearance_refs=[],
        qc_dimensions=["geometry_validity", "motion_continuity"],
        # 16.2：VERIFIED 必须引用能力证据。
        capability_refs=[f"route_validation_{uuid.uuid4().hex[:8]}"],
        status=ProductionRouteStatus.VERIFIED,
        created_by="ops_route_validation",
        tenant_id=_TENANT,
        organization_id=_TENANT,
    )
    async with gen_sm() as session:
        await apply_db_context(session, project_ids=(project_scope,))
        await SqlAlchemyProductionRouteRepository(session).add(route)
        await session.commit()
    return route.entity_id


async def _create_spec_and_plan(
    shot_sm: async_sessionmaker[AsyncSession],
    *,
    revision_id: str,
    shot_id: str,
    project_id: str,
) -> tuple[str, str]:
    """建立 ShotSpec + ShotVisualPlan（内部领域用例，见诚实边界 1）。"""
    principal = _service_principal()
    async with shot_sm() as session:
        await apply_db_context(session, project_ids=(project_id,))
        spec = await ShotSpecService(
            SqlAlchemyShotSpecRepository(session),
            SqlAlchemyShotRevisionRepository(session),
            SqlAlchemyOutboxWriter(session),
        ).create_spec(
            shot_revision_id=revision_id,
            duration_ms=4200,
            aspect_ratio="16:9",
            fps="24/1",
            coordinate_system="RIGHT_HANDED/Y/METER",
            camera_path={"type": "DOLLY", "keyframes": []},
            control_signal_refs=["camera_path"],
            principal=principal,
        )
        plan = await ShotVisualPlanService(
            SqlAlchemyShotVisualPlanRepository(session)
        ).create_plan(
            shot_id=shot_id,
            project_id=project_id,
            intent_summary="克制的推近，压迫感来自留白",
            camera_language="静态机位 + 缓推",
            shot_size="MEDIUM_CLOSE_UP",
            lens_range="50-85mm",
            motion_intent="缓慢前推 8% 画幅",
            lighting_intent="单侧窗光，高对比",
            open_fields=["lighting_intent", "lens_range"],
            principal=principal,
        )
        await session.commit()
    return spec.entity_id, plan.entity_id


async def _events_by_aggregate(
    gen_sm: async_sessionmaker[AsyncSession], aggregate_ids: list[str]
) -> dict[str, list[str]]:
    """按聚合 id 取审计事件类型（逐个核对，而不是只看事件总数）。"""
    async with gen_sm() as session:
        await apply_db_context(session)
        rows = (
            await session.execute(
                text(
                    "SELECT aggregate_id, event_type FROM outbox.outbox_events "
                    "WHERE aggregate_id = ANY(:ids) "
                    "ORDER BY occurred_at, event_type"
                ),
                {"ids": aggregate_ids},
            )
        ).all()
    events: dict[str, list[str]] = {aggregate_id: [] for aggregate_id in aggregate_ids}
    for aggregate_id, event_type in rows:
        events[aggregate_id].append(event_type)
    return events


# --------------------------------------------------------------------------- #
# 主链
# --------------------------------------------------------------------------- #
async def test_phase1_main_chain_end_to_end(
    shot_sm: async_sessionmaker[AsyncSession],
    gen_sm: async_sessionmaker[AsyncSession],
    asset_sm: async_sessionmaker[AsyncSession],
) -> None:
    """Phase 1 主链：建项目 → 交付清单 DRAFT，全链在真实约束下闭环。"""
    async with (
        _client("producer_001", ["制片"]) as producer,
        _client("writer_001", ["编剧"]) as writer,
        _client("agent_shot", ["Agent"], subject_type="agent") as agent_shot,
        _client("director_001", ["导演"]) as director,
        _client("qc_worker_001", ["QC Worker"]) as qc_worker,
        _client("reviewer_001", ["导演"]) as reviewer,
        # 交付清单的契约主体是「交付服务/授权制片」，制片（producer）不在
        # 允许集合内——用授权制片身份，顺带证明角色边界是按契约逐字生效的，
        # 而不是"挂着制片头衔就能交付"。
        _client("delivery_owner_001", ["授权制片"]) as delivery,
    ):
        # ------------------------------------------------------------------
        # 1) 项目创建（PROJECT.CREATE）
        # ------------------------------------------------------------------
        resp = await producer.post(
            "/projects", json={"project_name": "主链 E2E 项目", "project_tier": "B"}
        )
        assert resp.status_code == 201, resp.text
        project_id = resp.json()["entity_id"]
        assert resp.json()["status"] == "DRAFT"

        # ------------------------------------------------------------------
        # 2) 集数 + 剧本导入（EPISODE.CREATE / SCRIPT.IMPORT）
        # ------------------------------------------------------------------
        episode_ref = f"EP_{uuid.uuid4().hex[:6]}"
        resp = await producer.post(
            "/episodes",
            json={
                "project_id": project_id,
                "episode_ref": episode_ref,
                "title": "第一集",
            },
        )
        assert resp.status_code == 201, resp.text
        episode_id = resp.json()["entity_id"]

        resp = await writer.post(
            "/scripts/import",
            json={
                "project_id": project_id,
                "episode_ref": episode_ref,
                "title": "第一集剧本 v1",
            },
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["status"] == "DRAFT"
        script_id = resp.json()["entity_id"]

        # ------------------------------------------------------------------
        # 3) AI 拆镜草案（SHOT.DRAFT.CREATE，Agent 只能产草案）
        # ------------------------------------------------------------------
        shot_id = f"SH_{uuid.uuid4().hex[:6]}"
        resp = await agent_shot.post(
            "/shots/drafts",
            json={
                "project_id": project_id,
                "episode_ref": episode_ref,
                "scene_ref": "SC_01",
                "shot_code": "SHOT_010",
                "intent_summary": "主角发现门缝里的光",
                "shot_description": "中近景，缓推至门缝特写",
            },
        )
        assert resp.status_code == 201, resp.text
        draft = resp.json()
        assert draft["source"] == "agent"
        assert draft["status"] == "DRAFT"
        draft_id = draft["entity_id"]

        # ------------------------------------------------------------------
        # 4) 人工确认（SHOT.REVISION.CONFIRM，Agent 不得确认）
        # ------------------------------------------------------------------
        resp = await director.post(
            "/shots/revisions",
            json={
                "draft_id": draft_id,
                "shot_id": shot_id,
                "project_id": project_id,
            },
        )
        assert resp.status_code == 201, resp.text
        revision = resp.json()
        assert revision["revision"] == 1
        revision_id = revision["entity_id"]

        # ------------------------------------------------------------------
        # 5) ShotSpec + ShotVisualPlan（内部领域用例，见诚实边界 1）
        # ------------------------------------------------------------------
        spec_id, plan_id = await _create_spec_and_plan(
            shot_sm, revision_id=revision_id, shot_id=shot_id, project_id=project_id
        )

        # ------------------------------------------------------------------
        # 6) ConstraintPack：CREATE(DRAFT) → LOCK(ACTIVE)
        # ------------------------------------------------------------------
        resp = await director.post(
            "/shots/constraint-packs",
            json={
                "project_id": project_id,
                "shot_ids": [shot_id],
                "applicability": "APPLICABLE",
                "locked_dimensions": ["camera_path"],
                "constraints": [
                    {
                        "constraint_id": "constraint_001",
                        "target": "camera_path",
                        "shot_scope": [shot_id],
                        "rule": "机位不得穿过门窗平面",
                        "source": "director_brief_v1",
                        "authority_level": "DIRECTOR",
                        "valid_at": "2026-11-17T00:00:00Z",
                        "approval_ref": f"brief_{uuid.uuid4().hex[:8]}",
                    }
                ],
            },
        )
        assert resp.status_code == 201, resp.text
        pack = resp.json()
        assert pack["status"] == "DRAFT"
        pack_id = pack["constraint_pack_id"]

        # 负向：DRAFT 约束包不得作为生成输入（7.9「只有 ACTIVE 可作为生成输入」）。
        route_id = await _seed_verified_route(gen_sm, project_scope=project_id)
        rights_ref = f"rights_manifest_{uuid.uuid4().hex[:8]}"
        request_body = {
            "project_id": project_id,
            "shot_id": shot_id,
            "route_id": route_id,
            "shot_spec_ref": spec_id,
            "visual_plan_ref": plan_id,
            "constraint_snapshot_ref": pack_id,
            "budget_reservation_ref": f"budget_res_{uuid.uuid4().hex[:8]}",
            "quota_reservation_ref": f"quota_res_{uuid.uuid4().hex[:8]}",
            "dependency_refs": [],
            "rights_manifest_ref": rights_ref,
        }
        resp = await director.post(
            "/actions/generation.request", json=request_body
        )
        assert resp.status_code == 422, resp.text
        assert "ACTIVE" in resp.text

        # 正路：授权激活约束包后，同一请求通过。
        resp = await director.post(
            f"/shots/constraint-packs/{pack_id}/lock",
            json={"project_id": project_id},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "ACTIVE"

        # ------------------------------------------------------------------
        # 7) 选路（PRODUCTION.ROUTE.SELECT，路线须 VERIFIED）
        # ------------------------------------------------------------------
        resp = await producer.post(
            "/actions/production.route.select",
            json={
                "route_id": route_id,
                "project_id": project_id,
                "shot_id": shot_id,
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["audit_event"] == "PRODUCTION_ROUTE_SELECTED"
        selection_id = resp.json()["selection_id"]

        # 负向（C4 选路面）：同一镜头的第二条路线必须被拒绝，且不得静默排队。
        # 这是 Phase 1 验收清单里唯一以"并发"为口径的条目——这里不并发是为了
        # 让断言确定性；并发口径由 test_generation_route_api 的 gather 用例覆盖。
        second_route_id = await _seed_verified_route(gen_sm, project_scope=project_id)
        resp = await producer.post(
            "/actions/production.route.select",
            json={
                "route_id": second_route_id,
                "project_id": project_id,
                "shot_id": shot_id,
            },
        )
        assert resp.status_code == 409, resp.text
        assert resp.json()["error_code"] == "ROUTE_ALREADY_SELECTED"
        assert resp.json()["details"]["effective_route_id"] == route_id
        assert resp.json()["details"]["effective_selection_id"] == selection_id

        # ------------------------------------------------------------------
        # 8) 生成受理（GENERATION.REQUEST）
        # ------------------------------------------------------------------
        # 负向（C4 受理面）：在非生效路线上受理生成必须被拒——只守选路入口
        # 的话，"同一镜头在两个路线上各受理一次生成"仍然成立。
        resp = await director.post(
            "/actions/generation.request",
            json={**request_body, "route_id": second_route_id},
        )
        assert resp.status_code == 409, resp.text
        assert resp.json()["error_code"] == "ROUTE_NOT_EFFECTIVE"
        assert resp.json()["details"]["effective_route_id"] == route_id

        resp = await director.post("/actions/generation.request", json=request_body)
        assert resp.status_code == 200, resp.text
        accepted = resp.json()
        assert accepted["task_status"] == "PENDING"
        assert accepted["attempt"] == 1
        task_id = accepted["generation_task_id"]

        # ------------------------------------------------------------------
        # 9) worker 执行环：领取 → 生成 → 产出登记为资产版本 → 终态
        # ------------------------------------------------------------------
        stats = await execute_round(
            gen_sm,
            GenerationExecutorContext(
                tenant_id=_TENANT,
                organization_id=_TENANT,
                project_ids=frozenset({project_id}),
                worker_id="e2e-worker",
            ),
            DeterministicGenerationAdapter(),
            GenerationRoundStats(),
            registrar=AssetVersionRegistrar(asset_sessionmaker=asset_sm),
            lease_seconds=300.0,
        )
        assert (stats.claimed, stats.succeeded, stats.failed) == (1, 1, 0)

        async with gen_sm() as session:
            await apply_db_context(session, project_ids=(project_id,))
            task = await SqlAlchemyGenerationTaskRepository(session).get_by_id(task_id)
        assert task is not None
        assert task.status is GenerationTaskStatus.SUCCEEDED
        # 终态指向**产出对象**（资产版本），不是适配器内部引用串。
        asset_id = task.result_ref
        assert asset_id == f"av_{task_id}"

        async with asset_sm() as session:
            await apply_db_context(session, project_ids=(project_id,))
            asset = await SqlAlchemyAssetRepository(session).get_by_id(asset_id)
        assert asset is not None
        assert asset.status is AssetStatus.GENERATED
        assert asset.project_id == project_id
        assert asset.adapter_id == route_id
        assert asset.rights_manifest_ref == rights_ref
        assert len(asset.sha256) == 64

        # 负向：GENERATED（未过 QC / 未批准）的资产不得进交付清单。
        resp = await delivery.post(
            "/delivery-manifests",
            json={
                "project_id": project_id,
                "spec_ref": spec_id,
                "qc_final_ref": "qc_final_placeholder",
                "picture_lock_ref": "picture_lock_placeholder",
                "asset_refs": [asset_id],
                "rights_evidence_refs": [rights_ref],
                "rebuild_recipe": "recipe://main-chain/v1",
            },
        )
        assert resp.status_code == 422, resp.text
        assert "APPROVED" in resp.text

        # ------------------------------------------------------------------
        # 10) 核心 QC（QC.REPORT.CREATE → TECHNICAL_QC）
        # ------------------------------------------------------------------
        resp = await qc_worker.post(
            f"/assets/{asset_id}/qc-reports",
            json={
                "quality_profile_version": "qp_v1",
                "detector_versions": {"motion_continuity": "1.2.0"},
                "findings": [
                    {
                        "detector": "motion_continuity",
                        "verdict": "PASS",
                        "score": 0.94,
                    }
                ],
            },
        )
        assert resp.status_code == 201, resp.text
        qc_payload = resp.json()
        assert qc_payload["asset_version"]["status"] == "TECHNICAL_QC"
        qc_report_id = qc_payload["qc_report"]["qc_report_id"]

        # ------------------------------------------------------------------
        # 11) 人工批准（ASSET.VERSION.APPROVE，TECHNICAL_QC → HUMAN_REVIEW → APPROVED）
        # ------------------------------------------------------------------
        resp = await reviewer.post(f"/assets/{asset_id}/approve")
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "APPROVED"

        # ------------------------------------------------------------------
        # 12) 人工 Gate（PRODUCTION.GATE.APPROVE，Gate4，CRITICAL，人不得是 Agent）
        # ------------------------------------------------------------------
        gate_id = f"gate4_{uuid.uuid4().hex[:8]}"
        gate_body = {
            "entity_id": gate_id,
            "project_id": project_id,
            "gate_key": "GATE_4",
            "gate_instance_id": f"instance_{uuid.uuid4().hex[:8]}",
            "decision": "APPROVED",
            "constraint_snapshot_id": pack_id,
            "ruleset_version": 1,
            "entry_criteria": "核心 QC 完成、资产批准、权利证据齐备",
            "required_artifacts": "AssetVersion(APPROVED)、QCReport、RightsManifest",
            "reviewers": ["director_001"],
            "reason_code": "ALL_CHECKS_PASSED",
            "comment": "主链终检通过",
            "evidence_refs": [qc_report_id, rights_ref, asset_id],
            "scope": {"level": "shot", "shot_id": shot_id},
        }
        resp = await director.post("/gate-decisions", json=gate_body)
        assert resp.status_code == 201, resp.text
        assert resp.json()["decision"] == "APPROVED"
        assert resp.json()["decided_by"] == "director_001"

        # ------------------------------------------------------------------
        # 13) 交付清单（DELIVERY.MANIFEST.CREATE → DRAFT，服务端算校验和）
        # ------------------------------------------------------------------
        manifest_body = {
            "project_id": project_id,
            "spec_ref": spec_id,
            "qc_final_ref": qc_report_id,
            "picture_lock_ref": gate_id,  # 见诚实边界 4
            "asset_refs": [asset_id],
            "rights_evidence_refs": [rights_ref],
            "rebuild_recipe": "recipe://main-chain/v1",
        }
        # 角色边界：制片头衔不足以生成交付清单（契约主体是交付服务/授权制片）。
        resp = await producer.post("/delivery-manifests", json=manifest_body)
        assert resp.status_code == 403, resp.text

        resp = await delivery.post("/delivery-manifests", json=manifest_body)
        assert resp.status_code == 201, resp.text
        manifest = resp.json()
        assert manifest["status"] == "DRAFT"
        assert manifest["asset_refs"] == [asset_id]
        assert len(manifest["checksum"]) == 64

    # ---------------------------------------------------------------------- #
    # 审计链：每个环节都要有契约登记的事件，落到真实 outbox
    # ---------------------------------------------------------------------- #
    events = await _events_by_aggregate(
        gen_sm,
        [project_id, episode_id, script_id, draft_id, revision_id, pack_id,
         route_id, task_id, asset_id, gate_id, manifest["manifest_id"]],
    )
    assert "PROJECT_CREATED" in events[project_id]
    assert "EPISODE_CREATED" in events[episode_id]
    assert "SCRIPT_IMPORTED" in events[script_id]
    assert "SHOT_DRAFT_CREATED" in events[draft_id]
    assert "SHOT_REVISION_CONFIRMED" in events[revision_id]
    assert "CONSTRAINT_PACK_LOCKED" in events[pack_id]
    assert "PRODUCTION_ROUTE_SELECTED" in events[route_id]
    assert "GENERATION_REQUESTED" in events[task_id]
    # 资产链的四条事件必须齐全：创建 / QC / 进入人工复核 / 批准。
    # 只有"最终 APPROVED"是不够的：审计要能证明人工复核真的发生过。
    assert events[asset_id] == [
        "ASSET_VERSION_CREATED",
        "QC_REPORT_CREATED",
        "ASSET_VERSION_HUMAN_REVIEW_ENTERED",
        "ASSET_VERSION_APPROVED",
    ]
    assert "PRODUCTION_GATE_APPROVED" in events[gate_id]
    assert "DELIVERY_MANIFEST_CREATED" in events[manifest["manifest_id"]]
