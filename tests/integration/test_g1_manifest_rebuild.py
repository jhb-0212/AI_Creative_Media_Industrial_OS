"""G1 证据：用交付清单端到端重建，逐项比对，缺项显式列出。

验收口径（`Phase1-实现验收清单.md` G1）：
> 用清单重建一次交付；重建结果与原件哈希一致；缺证据时明确列出缺项，不静默补全。

与既有单元用例的分界：`test_delivery_manifest_domain` 证明的是"同一份内容
投影重算哈希相等"（纯函数性质）。那证明不了"清单里的引用还找得回来"。
本文件证明后者：把 `asset_refs` / `qc_final_ref` / `spec_ref` 逐个**从证据
仓储取回**（跨域的经所有者域端口，ADR-R28），只用取回成功的证据组装视图
再算校验和。

四个场景，失败模式各不相同：
1. 全部证据成立 → 校验和与原件一致、verified 计数逐项对得上；
2. QC 报告不存在 → 列入 `missing`，且校验和**必然不同**（不静默用引用串凑齐）；
3. QC 报告检测的是另一条资产 → 列入 `mismatched`（比缺失更危险：证据被换过）；
4. PictureLock / 权利证据 → 列入 `not_modeled`，让 `complete=False` 可解释。

诚实边界（不证的部分）：字节级内容校验（按 `storage_ref.uri` 取回文件重算
sha256）当前做不到——平台没有对象存储适配器（`adapters/storage/` 只有
`.gitkeep`）。所以 `VERIFIED` 的含义是"元数据自洽且归属与状态正确"，
不是"内容哈希已验证"。这条写进 ADR-R33 §4 债务，不在这里冒充。
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.api.main import app
from packages.shared.outbox_writer import SqlAlchemyOutboxWriter
from services.asset_service.repository.sqlalchemy_asset_repo import (
    SqlAlchemyAssetRepository,
)
from services.asset_service.repository.sqlalchemy_manifest_repo import (
    SqlAlchemyDeliveryManifestRepository,
)
from services.asset_service.repository.sqlalchemy_qc_repo import (
    SqlAlchemyQCReportRepository,
)
from services.asset_service.service.manifest_rebuild_service import (
    DeliveryManifestRebuildService,
    EvidenceRefType,
    EvidenceState,
)
from services.shot_planning.repository.shot_revision_sqlalchemy_repo import (
    SqlAlchemyShotRevisionRepository,
)
from services.shot_planning.repository.shot_spec_sqlalchemy_repo import (
    SqlAlchemyShotSpecRepository,
)
from services.shot_planning.service.spec_service import ShotSpecService
from tests.conftest import TEST_TENANT_ID, apply_db_context, make_test_headers

pytestmark = pytest.mark.asyncio


def _client(subject: str, roles: list[str], *, subject_type: str = "human") -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers=make_test_headers(
            subject_id=subject, roles=roles, subject_type=subject_type
        ),
    )


async def _seed_assets(
    *,
    qc_worker: AsyncClient,
    director: AsyncClient,
    project_id: str,
    count: int = 2,
) -> list[tuple[str, str]]:
    """建若干"已批准"的资产版本，返回 [(asset_id, qc_report_id)]。"""
    created: list[tuple[str, str]] = []
    for _ in range(count):
        asset_id = f"av_g1_{uuid.uuid4().hex[:8]}"
        resp = await qc_worker.post(
            "/assets",
            json={
                "project_id": project_id,
                "asset_type": "VIDEO",
                "sha256": uuid.uuid4().hex + uuid.uuid4().hex,
                "storage_ref": {"uri": f"s3://ai-media/g1/{asset_id}.mp4"},
                "entity_id": asset_id,
            },
        )
        assert resp.status_code == 201, resp.text

        resp = await qc_worker.post(
            f"/assets/{asset_id}/qc-reports",
            json={
                "quality_profile_version": "qp_v1",
                "detector_versions": {"motion_continuity": "1.2.0"},
                "findings": [
                    {"detector": "motion_continuity", "verdict": "PASS", "score": 0.93}
                ],
            },
        )
        assert resp.status_code == 201, resp.text
        report_id = resp.json()["qc_report"]["qc_report_id"]

        resp = await director.post(f"/assets/{asset_id}/approve")
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "APPROVED"
        created.append((asset_id, report_id))
    return created


async def _seed_spec(
    *,
    shot_sm: async_sessionmaker[AsyncSession],
    agent_shot: AsyncClient,
    director: AsyncClient,
    project_id: str,
) -> str:
    """建一条 ShotSpec（草案 → 人工确认 → 规格），返回 spec_id。

    ShotSpec 没有 HTTP 动作（主链 E2E 的诚实边界 1），这里同样走领域服务，
    但输入全部来自真实入口产出的 draft/revision id。
    """
    shot_id = f"SH_{uuid.uuid4().hex[:6]}"
    resp = await agent_shot.post(
        "/shots/drafts",
        json={
            "project_id": project_id,
            "episode_ref": "EP_01",
            "scene_ref": "SC_01",
            "shot_code": "SHOT_010",
            "intent_summary": "主角发现门缝里的光",
            "shot_description": "中近景，缓推",
        },
    )
    assert resp.status_code == 201, resp.text
    draft_id = resp.json()["entity_id"]

    resp = await director.post(
        "/shots/revisions",
        json={"draft_id": draft_id, "shot_id": shot_id, "project_id": project_id},
    )
    assert resp.status_code == 201, resp.text
    revision_id = resp.json()["entity_id"]

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
            principal=_service_principal(),
        )
        await session.commit()
    return spec.entity_id


def _service_principal() -> Any:
    from packages.identity.principal import Principal, SubjectType

    return Principal(
        subject_id="g1_director",
        subject_type=SubjectType.HUMAN,
        tenant_id=TEST_TENANT_ID,
        organization_id=TEST_TENANT_ID,
        roles=frozenset({"导演"}),
        project_ids=frozenset({"*"}),
    )


def _rebuild_service(asset_sm: async_sessionmaker[AsyncSession]) -> Any:
    """装配重建服务：会话由测试持有（只读，不 commit）。"""
    session = asset_sm()
    return session, DeliveryManifestRebuildService(
        manifests=SqlAlchemyDeliveryManifestRepository(session),
        assets=SqlAlchemyAssetRepository(session),
        qc_reports=SqlAlchemyQCReportRepository(session),
        # 端口来自组合根（与生产同一实现），不是测试自造的替身。
        spec_evidence=app.state.spec_evidence_port,
    )


async def _create_manifest(
    *,
    delivery: AsyncClient,
    project_id: str,
    asset_refs: list[str],
    qc_final_ref: str,
    spec_ref: str,
) -> dict[str, Any]:
    rights_ref = f"rights_manifest_{uuid.uuid4().hex[:8]}"
    resp = await delivery.post(
        "/delivery-manifests",
        json={
            "project_id": project_id,
            "spec_ref": spec_ref,
            "qc_final_ref": qc_final_ref,
            "picture_lock_ref": f"picture_lock_{uuid.uuid4().hex[:8]}",
            "asset_refs": asset_refs,
            "rights_evidence_refs": [rights_ref],
            "rebuild_recipe": "recipe://g1/v1",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_rebuild_resolves_every_reference_and_matches_checksum(
    asset_sm: async_sessionmaker[AsyncSession],
    shot_sm: async_sessionmaker[AsyncSession],
) -> None:
    """全部证据成立时：逐项 verified，重建校验和与原件一致。"""
    async with (
        _client("g1_producer", ["制片"]) as producer,
        _client("g1_qc", ["QC Worker"]) as qc_worker,
        _client("g1_agent", ["Agent"], subject_type="agent") as agent_shot,
        _client("g1_director", ["导演"]) as director,
        _client("g1_delivery", ["授权制片"]) as delivery,
    ):
        resp = await producer.post(
            "/projects", json={"project_name": "G1 重建项目", "project_tier": "B"}
        )
        assert resp.status_code == 201, resp.text
        project_id = resp.json()["entity_id"]

        assets = await _seed_assets(
            qc_worker=qc_worker, director=director, project_id=project_id, count=2
        )
        spec_id = await _seed_spec(
            shot_sm=shot_sm,
            agent_shot=agent_shot,
            director=director,
            project_id=project_id,
        )
        asset_refs = [asset_id for asset_id, _ in assets]
        manifest = await _create_manifest(
            delivery=delivery,
            project_id=project_id,
            asset_refs=asset_refs,
            qc_final_ref=assets[0][1],
            spec_ref=spec_id,
        )

        session, service = _rebuild_service(asset_sm)
        try:
            report = await service.rebuild(manifest["manifest_id"])
        finally:
            await session.close()

    # 1) 校验和一致（重建结果 == 原件）
    assert report.checksum_match, (
        f"重建校验和 {report.rebuilt_checksum} != 原件 {report.original_checksum}"
    )
    assert report.original_checksum == manifest["checksum"]

    # 2) 逐项：2 资产 + 1 QC + 1 规格 = 4 条 verified，且类型分布正确
    verified_types = sorted(item.ref_type.value for item in report.verified)
    assert verified_types == ["asset_version", "asset_version", "qc_report", "shot_spec"], (
        f"verified 证据集合不符: {verified_types}"
    )
    # 3) 缺项为空；未建模的两类显式列出（不是静默跳过）
    assert report.missing == ()
    assert {item.ref_type for item in report.not_modeled} == {
        EvidenceRefType.PICTURE_LOCK,
        EvidenceRefType.RIGHTS_EVIDENCE,
    }
    # 4) complete 必须为 False：两类证据未建模，就不能说"完整重建"
    assert report.complete is False
    # 5) 每条 verified 都带着可比对事实（不然"比对"只是走个过场）
    for item in report.verified:
        assert item.facts, f"{item.ref_type} 没有事实可比对"
    asset_items = [i for i in report.verified if i.ref_type is EvidenceRefType.ASSET_VERSION]
    assert all(len(i.facts["sha256"]) == 64 for i in asset_items)
    assert all(i.facts["status"] in {"APPROVED", "GOLD"} for i in asset_items)


async def test_missing_qc_report_is_listed_and_changes_the_checksum(
    asset_sm: async_sessionmaker[AsyncSession],
    shot_sm: async_sessionmaker[AsyncSession],
) -> None:
    """QC 报告不存在 → 列 missing，且校验和不再相等（不静默补全）。"""
    async with (
        _client("g1b_producer", ["制片"]) as producer,
        _client("g1b_qc", ["QC Worker"]) as qc_worker,
        _client("g1b_agent", ["Agent"], subject_type="agent") as agent_shot,
        _client("g1b_director", ["导演"]) as director,
        _client("g1b_delivery", ["授权制片"]) as delivery,
    ):
        resp = await producer.post(
            "/projects", json={"project_name": "G1 缺证据项目", "project_tier": "B"}
        )
        assert resp.status_code == 201, resp.text
        project_id = resp.json()["entity_id"]

        assets = await _seed_assets(
            qc_worker=qc_worker, director=director, project_id=project_id, count=1
        )
        spec_id = await _seed_spec(
            shot_sm=shot_sm,
            agent_shot=agent_shot,
            director=director,
            project_id=project_id,
        )
        # CREATE 不校验 qc_final_ref 是否存在（它是引用，不是外键）——
        # 正是这种"创建时成立、事后可能消失"的引用，才需要重建来取证。
        manifest = await _create_manifest(
            delivery=delivery,
            project_id=project_id,
            asset_refs=[assets[0][0]],
            qc_final_ref="qc_report_ghost",
            spec_ref=spec_id,
        )

        session, service = _rebuild_service(asset_sm)
        try:
            report = await service.rebuild(manifest["manifest_id"])
        finally:
            await session.close()

    assert [item.ref for item in report.missing] == ["qc_report_ghost"]
    assert report.missing[0].state is EvidenceState.MISSING
    assert report.missing[0].ref_type is EvidenceRefType.QC_REPORT
    assert report.checksum_match is False, "缺证据时校验和必须不同，否则重建是空转"
    assert report.complete is False


async def test_qc_report_of_another_asset_is_flagged_as_mismatch(
    asset_sm: async_sessionmaker[AsyncSession],
    shot_sm: async_sessionmaker[AsyncSession],
) -> None:
    """QC 报告存在但检测的不是本清单资产 → mismatched（证据被换过）。"""
    async with (
        _client("g1c_producer", ["制片"]) as producer,
        _client("g1c_qc", ["QC Worker"]) as qc_worker,
        _client("g1c_agent", ["Agent"], subject_type="agent") as agent_shot,
        _client("g1c_director", ["导演"]) as director,
        _client("g1c_delivery", ["授权制片"]) as delivery,
    ):
        resp = await producer.post(
            "/projects", json={"project_name": "G1 错配证据项目", "project_tier": "B"}
        )
        assert resp.status_code == 201, resp.text
        project_id = resp.json()["entity_id"]

        assets = await _seed_assets(
            qc_worker=qc_worker, director=director, project_id=project_id, count=2
        )
        spec_id = await _seed_spec(
            shot_sm=shot_sm,
            agent_shot=agent_shot,
            director=director,
            project_id=project_id,
        )
        # 只交付第一条资产，却引用第二条资产的 QC 报告。
        manifest = await _create_manifest(
            delivery=delivery,
            project_id=project_id,
            asset_refs=[assets[0][0]],
            qc_final_ref=assets[1][1],
            spec_ref=spec_id,
        )

        session, service = _rebuild_service(asset_sm)
        try:
            report = await service.rebuild(manifest["manifest_id"])
        finally:
            await session.close()

    mismatched = [i for i in report.items if i.state is EvidenceState.MISMATCHED]
    assert len(mismatched) == 1, [i.to_dict() for i in report.items]
    assert mismatched[0].ref_type is EvidenceRefType.QC_REPORT
    assert mismatched[0].facts["asset_version_id"] == assets[1][0]
    assert report.checksum_match is False
    assert report.complete is False
