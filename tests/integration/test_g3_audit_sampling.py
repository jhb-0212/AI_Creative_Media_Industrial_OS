"""G3 证据：十个写操作的审计抽样（catalog 驱动，不硬编码字段名）。

验收口径（`Phase1-实现验收清单.md` G3）：
> 抽样 10 个写操作查审计；全部有调用主体、目标对象、前后状态；审计不可被业务侧删除。

为什么断言方式必须由 `contracts/runtime/event_catalog.yaml` 驱动：
事件载荷该有哪些字段，契约已经逐条登记在 `payload_required` 里。如果在测试里
再写一份 `{"created_by", "from_status", ...}` 的字面量，就出现了两份权威：
契约加字段不会让测试变严，代码漏字段也不会被契约发现——两边一起漂移时测试
仍然全绿。这里的唯一事实源是 catalog，测试只负责"实际发出来的 payload ⊇
契约声明的最小字段集"。

为什么还要单独测 outbox 的权限：
"审计不可被业务侧删除"不是靠应用层不写 DELETE 语句，而是靠**数据库授权**。
`outbox.outbox_events` 对每个业务域角色只授 SELECT / INSERT；relay 角色额外
被授予 4 个投递列的 UPDATE（`published_at` / `attempts` / `next_attempt_at` /
`dead_lettered_at`），`payload` 等事实列仍不可改。这四条边界要分别证明，
否则"不能删"可能只是"没写删除代码"。

两处刻意不做的断言：
- 不断言"每个事件都有 from_status/to_status"：创建类事件（如 `PROJECT_CREATED`）
  契约只要求 `status`，把"创建"和"转移"强行统一会让契约失真。
- 不用超级用户跑业务写操作：那样证明不了"业务角色写入路径"。
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
import yaml
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from apps.api.main import app
from packages.shared.database import build_migration_url
from tests.conftest import apply_db_context, make_test_headers

pytestmark = pytest.mark.asyncio

_REPO_ROOT = Path(__file__).resolve().parents[2]
CATALOG = _REPO_ROOT / "contracts" / "runtime" / "event_catalog.yaml"

#: 抽样的 10 个写操作：(action_key, HTTP 路径, 期望事件)。
#: 只登记"动作 → 事件"的对应关系，不登记任何 payload 字段名——字段名来自 catalog。
_SAMPLES: tuple[tuple[str, str, str], ...] = (
    ("PROJECT.CREATE", "POST /projects", "PROJECT_CREATED"),
    ("SCRIPT.IMPORT", "POST /scripts/import", "SCRIPT_IMPORTED"),
    ("SHOT.DRAFT.CREATE", "POST /shots/drafts", "SHOT_DRAFT_CREATED"),
    ("SHOT.REVISION.CONFIRM", "POST /shots/revisions", "SHOT_REVISION_CONFIRMED"),
    ("CONSTRAINT.PACK.CREATE", "POST /shots/constraint-packs", "CONSTRAINT_PACK_CREATED"),
    ("ASSET.VERSION.CREATE", "POST /assets", "ASSET_VERSION_CREATED"),
    ("QC.REPORT.CREATE", "POST /assets/{id}/qc-reports", "QC_REPORT_CREATED"),
    ("ASSET.VERSION.APPROVE", "POST /assets/{id}/approve", "ASSET_VERSION_APPROVED"),
    ("QC.REVIEW.OPEN", "POST /qc-review-sessions", "QC_REVIEW_OPENED"),
    (
        "DELIVERY.MANIFEST.CREATE",
        "POST /delivery-manifests",
        "DELIVERY_MANIFEST_CREATED",
    ),
)

#: outbox 的投递列（relay 可改），与事实列（任何人不可改）的分界。
_RELAY_UPDATABLE = ("published_at", "attempts", "next_attempt_at", "dead_lettered_at")


@pytest.fixture(scope="module")
async def admin_engine() -> AsyncEngine:
    """迁移角色连库：只用于读取事件 id 与证明"普通角色改不了"。"""
    engine = create_async_engine(build_migration_url(), pool_pre_ping=True)
    try:
        yield engine
    finally:
        await engine.dispose()


def _client(subject: str, roles: list[str], *, subject_type: str = "human") -> AsyncClient:
    """真实鉴权客户端（访问令牌 + capability 双凭证）。"""
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers=make_test_headers(
            subject_id=subject, roles=roles, subject_type=subject_type
        ),
    )


def _catalog_payload_required() -> dict[str, tuple[str, ...]]:
    """加载契约声明的事件最小字段集（唯一事实源）。"""
    data = yaml.safe_load(CATALOG.read_text(encoding="utf-8"))
    return {
        entry["event_type"]: tuple(entry["payload_required"])
        for entry in data["events"]
    }


async def _event_payload(
    *,
    domain_session: async_sessionmaker[AsyncSession],
    event_type: str,
    aggregate_id: str,
) -> dict[str, Any]:
    """按 (事件类型, 聚合 id) 取一条事件正文。"""
    async with domain_session() as session:
        await apply_db_context(session)
        row = (
            await session.execute(
                sa.text(
                    "SELECT payload FROM outbox.outbox_events "
                    "WHERE event_type = :et AND aggregate_id = :aid "
                    "ORDER BY occurred_at DESC LIMIT 1"
                ),
                {"et": event_type, "aid": aggregate_id},
            )
        ).scalar_one_or_none()
    assert row is not None, f"未找到事件 {event_type} / 聚合 {aggregate_id}（审计缺失）"
    return dict(row)


async def test_ten_write_operations_emit_catalog_complete_payloads(
    project_sm: async_sessionmaker[AsyncSession],
    asset_sm: async_sessionmaker[AsyncSession],
    shot_sm: async_sessionmaker[AsyncSession],
    qc_sm: async_sessionmaker[AsyncSession],
) -> None:
    """十个写操作的真实入口 → 事件正文覆盖 catalog 的 payload_required。"""
    required = _catalog_payload_required()
    # 抽样集合本身要可校验：catalog 里必须有这些事件，且必须恰好 10 个样本。
    missing_contract = [event for _, _, event in _SAMPLES if event not in required]
    assert not missing_contract, f"抽样事件未在 event_catalog 登记: {missing_contract}"
    assert len(_SAMPLES) == 10, "G3 口径是抽样 10 个写操作"
    assert len({action for action, _, _ in _SAMPLES}) == 10, "抽样动作重复"

    collected: list[tuple[str, str, str, async_sessionmaker[AsyncSession]]] = []

    async with (
        _client("g3_producer", ["制片"]) as producer,
        _client("g3_writer", ["编剧"]) as writer,
        _client("g3_agent", ["Agent"], subject_type="agent") as agent_shot,
        _client("g3_director", ["导演"]) as director,
        _client("g3_qc_worker", ["QC Worker"]) as qc_worker,
        _client("g3_qc_reviewer", ["QC 审核人"]) as qc_reviewer,
        _client("g3_delivery", ["授权制片"]) as delivery,
    ):
        # 1) PROJECT.CREATE
        resp = await producer.post(
            "/projects", json={"project_name": "G3 审计抽样项目", "project_tier": "B"}
        )
        assert resp.status_code == 201, resp.text
        project_id = resp.json()["entity_id"]
        collected.append(("PROJECT.CREATE", "PROJECT_CREATED", project_id, project_sm))

        # 2) SCRIPT.IMPORT
        episode_ref = f"EP_{uuid.uuid4().hex[:6]}"
        resp = await writer.post(
            "/scripts/import",
            json={
                "project_id": project_id,
                "episode_ref": episode_ref,
                "title": "G3 抽样剧本",
            },
        )
        assert resp.status_code == 201, resp.text
        script_id = resp.json()["entity_id"]
        collected.append(("SCRIPT.IMPORT", "SCRIPT_IMPORTED", script_id, project_sm))

        # 3) SHOT.DRAFT.CREATE（Agent 只产草案）
        shot_id = f"SH_{uuid.uuid4().hex[:6]}"
        resp = await agent_shot.post(
            "/shots/drafts",
            json={
                "project_id": project_id,
                "episode_ref": episode_ref,
                "scene_ref": "SC_01",
                "shot_code": "SHOT_010",
                "intent_summary": "主角发现门缝里的光",
                "shot_description": "中近景，缓推",
            },
        )
        assert resp.status_code == 201, resp.text
        draft_id = resp.json()["entity_id"]
        collected.append(("SHOT.DRAFT.CREATE", "SHOT_DRAFT_CREATED", draft_id, shot_sm))

        # 4) SHOT.REVISION.CONFIRM（人类确认）
        resp = await director.post(
            "/shots/revisions",
            json={"draft_id": draft_id, "shot_id": shot_id, "project_id": project_id},
        )
        assert resp.status_code == 201, resp.text
        revision_id = resp.json()["entity_id"]
        collected.append(
            ("SHOT.REVISION.CONFIRM", "SHOT_REVISION_CONFIRMED", revision_id, shot_sm)
        )

        # 5) CONSTRAINT_PACK.CREATE
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
        pack_id = resp.json()["constraint_pack_id"]
        collected.append(
            ("CONSTRAINT.PACK.CREATE", "CONSTRAINT_PACK_CREATED", pack_id, shot_sm)
        )

        # 6) ASSET.VERSION.CREATE（走真实入口登记资产，不直插）
        asset_id = f"av_g3_{uuid.uuid4().hex[:8]}"
        resp = await qc_worker.post(
            "/assets",
            json={
                "project_id": project_id,
                "asset_type": "VIDEO",
                "sha256": "c" * 64,
                "storage_ref": {"uri": f"s3://ai-media/g3/{asset_id}.mp4"},
                "source_asset_version_ids": [],
                "entity_id": asset_id,
            },
        )
        assert resp.status_code == 201, resp.text
        collected.append(("ASSET.VERSION.CREATE", "ASSET_VERSION_CREATED", asset_id, asset_sm))

        # 7) QC.REPORT.CREATE
        resp = await qc_worker.post(
            f"/assets/{asset_id}/qc-reports",
            json={
                "quality_profile_version": "qp_v1",
                "detector_versions": {"motion_continuity": "1.2.0"},
                "findings": [
                    {"detector": "motion_continuity", "verdict": "PASS", "score": 0.94}
                ],
            },
        )
        assert resp.status_code == 201, resp.text
        qc_report_id = resp.json()["qc_report"]["qc_report_id"]
        collected.append(("QC.REPORT.CREATE", "QC_REPORT_CREATED", asset_id, asset_sm))

        # 8) ASSET.VERSION.APPROVE（TECHNICAL_QC → HUMAN_REVIEW → APPROVED）
        resp = await director.post(f"/assets/{asset_id}/approve")
        assert resp.status_code == 200, resp.text
        collected.append(
            ("ASSET.VERSION.APPROVE", "ASSET_VERSION_APPROVED", asset_id, asset_sm)
        )

        # 9) QC.REVIEW.OPEN（缺陷九项必填，QC 报告必须真实存在）
        session_id = f"qcrs_{uuid.uuid4().hex[:8]}"
        resp = await qc_reviewer.post(
            "/qc-review-sessions",
            json={
                "project_id": project_id,
                "asset_version_id": asset_id,
                "qc_report_ref": qc_report_id,
                "reviewers": ["g3_qc_reviewer", "g3_director"],
                "findings": [
                    {
                        "issue_id": "i1",
                        "shot_id": shot_id,
                        "asset_version": 1,
                        "timecode": "00:00:03:12",
                        "frame_range": "84-96",
                        "qc_dimension": "VISUAL",
                        "severity": "MAJOR",
                        "evidence": "frame_0087.png",
                        "detector_version": 3,
                        "human_decision": "PENDING",
                        "disposition": "PENDING",
                    }
                ],
                "session_id": session_id,
            },
        )
        assert resp.status_code == 201, resp.text
        collected.append(("QC.REVIEW.OPEN", "QC_REVIEW_OPENED", session_id, qc_sm))

        # 10) DELIVERY.MANIFEST.CREATE（只接受 APPROVED/GOLD 资产）
        rights_ref = f"rights_manifest_{uuid.uuid4().hex[:8]}"
        resp = await delivery.post(
            "/delivery-manifests",
            json={
                "project_id": project_id,
                "spec_ref": revision_id,
                "qc_final_ref": qc_report_id,
                "picture_lock_ref": pack_id,
                "asset_refs": [asset_id],
                "rights_evidence_refs": [rights_ref],
                "rebuild_recipe": "recipe://g3-sampling/v1",
            },
        )
        assert resp.status_code == 201, resp.text
        manifest_id = resp.json()["manifest_id"]
        collected.append(
            ("DELIVERY.MANIFEST.CREATE", "DELIVERY_MANIFEST_CREATED", manifest_id, asset_sm)
        )

        # ------------------------------------------------------------------ #
        # 断言：逐条比对 catalog 的 payload_required
        # ------------------------------------------------------------------ #
        problems: list[str] = []
        for action_key, event_type, aggregate_id, domain_session in collected:
            payload = await _event_payload(
                domain_session=domain_session,
                event_type=event_type,
                aggregate_id=aggregate_id,
            )
            expected = required[event_type]
            missing = [field for field in expected if field not in payload]
            if missing:
                problems.append(f"{action_key} / {event_type} 缺字段: {missing}")
            # 目标对象：聚合 id 必须出现在正文里（否则审计无法从事件回到对象）。
            if aggregate_id not in {str(value) for value in payload.values()}:
                problems.append(
                    f"{action_key} / {event_type} 正文不含聚合 id {aggregate_id}"
                )
        assert not problems, "审计抽样不达标：\n" + "\n".join(problems)
        assert len(collected) == 10


async def test_outbox_facts_are_immutable_for_business_roles(
    admin_engine: AsyncEngine,
    project_sm: async_sessionmaker[AsyncSession],
    asset_sm: async_sessionmaker[AsyncSession],
    shot_sm: async_sessionmaker[AsyncSession],
    qc_sm: async_sessionmaker[AsyncSession],
    gen_sm: async_sessionmaker[AsyncSession],
    workflow_sm: async_sessionmaker[AsyncSession],
    relay_sm: async_sessionmaker[AsyncSession],
) -> None:
    """业务角色对 outbox 无 UPDATE / DELETE；relay 只能改投递列。"""
    async with admin_engine.begin() as conn:
        event_id = (
            await conn.execute(
                sa.text("SELECT event_id FROM outbox.outbox_events ORDER BY occurred_at DESC LIMIT 1")
            )
        ).scalar_one_or_none()
    assert event_id is not None, "outbox 为空，无法取证审计不可删改"

    business_roles = {
        "project_planning": project_sm,
        "asset": asset_sm,
        "shot_planning": shot_sm,
        "qc_governance": qc_sm,
        "generation_execution": gen_sm,
        "workflow": workflow_sm,
    }
    mutations = {
        "DELETE": "DELETE FROM outbox.outbox_events WHERE event_id = :e",
        "UPDATE payload": "UPDATE outbox.outbox_events SET payload = payload WHERE event_id = :e",
        "UPDATE event_type": (
            "UPDATE outbox.outbox_events SET event_type = event_type WHERE event_id = :e"
        ),
    }

    allowed: list[str] = []
    for role_name, sessionmaker in business_roles.items():
        for label, sql in mutations.items():
            try:
                async with sessionmaker() as session:
                    await apply_db_context(session)
                    await session.execute(sa.text(sql), {"e": str(event_id)})
                    await session.commit()
            except DBAPIError as exc:
                # 权限拒绝发生在计划阶段：与"0 行受影响"不是同一件事。
                assert "permission denied" in str(exc), (
                    f"{role_name} 的 {label} 未被权限拒绝，实际错误: {exc}"
                )
            else:
                allowed.append(f"{role_name} 可以 {label}")
    assert not allowed, "以下业务角色能改动审计事件：\n" + "\n".join(allowed)

    # relay：投递列可改（否则投递不可能），事实列不可改。
    for column in _RELAY_UPDATABLE:
        async with relay_sm() as session:
            await apply_db_context(session)
            await session.execute(
                sa.text(
                    f"UPDATE outbox.outbox_events SET {column} = {column} WHERE event_id = :e"
                ),
                {"e": str(event_id)},
            )
            await session.commit()

    for sql in (
        "UPDATE outbox.outbox_events SET payload = payload WHERE event_id = :e",
        "DELETE FROM outbox.outbox_events WHERE event_id = :e",
    ):
        with pytest.raises(DBAPIError) as exc_info:
            async with relay_sm() as session:
                await apply_db_context(session)
                await session.execute(sa.text(sql), {"e": str(event_id)})
                await session.commit()
        assert "permission denied" in str(exc_info.value), str(exc_info.value)


async def test_sampled_actions_are_registered_and_implemented() -> None:
    """抽样动作必须是契约里登记过的 action_key（否则抽的是不存在的能力）。"""
    registry = yaml.safe_load(
        (_REPO_ROOT / "contracts" / "action_registry" / "action_registry.yaml").read_text(
            encoding="utf-8"
        )
    )
    keys = {entry["action_key"] for entry in registry["actions"]}
    unknown = [action for action, _, _ in _SAMPLES if action not in keys]
    assert not unknown, f"抽样动作未在 action_registry 登记: {unknown}"
