"""QC 复查 API 集成测试（真实 PostgreSQL + 真实 JWT 鉴权 + 真实域角色）。

C5（Phase 1 验收：QC 缺陷闭环）的端到端证据，逐条对应契约与裁决：

1. **缺陷九项必须成立**（21.1）：缺 evidence 的缺陷不得开启会话 → 422。
2. **证据不得跨项目/跨版本漂流**：报告归属与请求不符 → 422；报告不存在 → 404。
3. **状态链只能沿登记边推进**（C.7）：RESOLVED→CLOSED 跳关 → 409，且响应里
   直接给出"你现在只能去 GATE_ELIGIBLE"（31.5 裁决 ④）。
4. **结论只追加、最新一条生效**：同一缺陷第二次裁决不会覆盖历史。
5. **身份不由请求体决定**：DECIDE 请求体里塞 decided_by 不生效。
6. **AI 不能单独裁决**（21.2）：Agent 主体带齐全角色也拿不到结论（领域层拒）。
7. **并发裁决按 entity_version CAS**：后写者拿到 CONCURRENT_MODIFICATION。
8. **数据库层独立兜底**（ADR-R20）：直连 SQL 的跳关 / 改写 findings / 删除
   会话全部被守卫触发器拒绝——不依赖任何应用代码记得校验。
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
import sqlalchemy as sa
from httpx import AsyncClient
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.identity.principal import Principal, SubjectType
from packages.shared.database import build_migration_url
from services.qc_governance.repository.sqlalchemy_qc_review_session_repo import (
    SqlAlchemyQCReviewSessionRepository,
)
from tests.conftest import (
    TEST_TENANT_ID,
    apply_db_context,
    make_test_headers,
    set_raw_rls_context,
)

pytestmark = pytest.mark.asyncio

#: 每测试一个唯一项目：RLS 按项目过滤，固定项目会让第二次运行读到第一次的
#: 会话（一报告一会话的唯一约束会让用例从"开启成功"变成 409）。
_QC_ROLES = ["QC 审核人", "导演"]


@pytest.fixture(scope="module")
async def admin_engine() -> AsyncEngine:
    """迁移角色（超级用户）连库：只用于播种证据与验证数据库守卫。

    刻意用超级用户而不是业务角色：守卫触发器若只对业务角色生效，就变成
    "权限挡住了"而不是"结构挡住了"——那两种结论完全不同（ADR-R20）。
    """
    engine = create_async_engine(build_migration_url(), pool_pre_ping=True)
    try:
        yield engine
    finally:
        await engine.dispose()


def _headers(
    subject_id: str = "qc_reviewer",
    *,
    roles: list[str] | None = None,
    subject_type: str = "human",
) -> dict[str, str]:
    return make_test_headers(
        subject_id=subject_id,
        roles=roles or _QC_ROLES,
        subject_type=subject_type,
    )


async def _seed_report(
    admin_engine: AsyncEngine,
    *,
    project_id: str,
    report_id: str | None = None,
) -> tuple[str, str]:
    """播种一条资产版本 + 一份 QC 报告（超级用户直插，绕过 RLS 与授权）。

    返回 ``(asset_version_id, qc_report_id)``。QC 报告必须挂在真实资产版本上
    （外键锁死），否则"报告引用"这条校验证明不了任何东西。
    """
    asset_version_id = f"av_qc_{uuid.uuid4().hex[:12]}"
    qc_report_id = report_id or f"qcr_{uuid.uuid4().hex[:12]}"
    async with admin_engine.begin() as conn:
        await conn.execute(
            sa.text(
                """
                INSERT INTO asset_service.asset_versions (
                    entity_id, entity_type, schema_version, entity_version,
                    status, owner_module, tenant_id, organization_id,
                    created_at, created_by, updated_at, audit_ref,
                    asset_type, sha256, storage_ref, source_asset_version_ids,
                    project_id
                ) VALUES (
                    :aid, 'AssetVersion', 205, 1,
                    'TECHNICAL_QC', 'asset_service', :tid, :tid,
                    now(), 'qc_seed', now(), NULL,
                    'VIDEO', repeat('b', 64), '{}'::jsonb, '[]'::jsonb,
                    :pid
                )
                """
            ),
            {
                "aid": asset_version_id,
                "tid": TEST_TENANT_ID,
                "pid": project_id,
            },
        )
        await conn.execute(
            sa.text(
                """
                INSERT INTO asset_service.qc_reports (
                    qc_report_id, asset_version_id, tenant_id, organization_id,
                    project_id, quality_profile_version, benchmark_version,
                    detector_versions, findings, deviation_class,
                    deviation_intent_ref, intent_match_status, human_decision,
                    created_by, created_at, updated_at
                ) VALUES (
                    :rid, :aid, :tid, :tid,
                    :pid, 'qp_v1', NULL,
                    '{"visual": 3}'::jsonb, '[{"issue_id": "i1"}]'::jsonb, NULL,
                    NULL, NULL, NULL,
                    'qc_detector', now(), now()
                )
                """
            ),
            {
                "rid": qc_report_id,
                "aid": asset_version_id,
                "tid": TEST_TENANT_ID,
                "pid": project_id,
            },
        )
    return asset_version_id, qc_report_id


def _finding(issue_id: str = "i1", **overrides: Any) -> dict[str, Any]:
    """一条满足 21.1 九项绑定的缺陷（含检测器多给的 confidence）。"""
    payload: dict[str, Any] = {
        "issue_id": issue_id,
        "shot_id": "SHOT_001",
        "asset_version": 1,
        "timecode": "00:00:03:12",
        "frame_range": "84-96",
        "qc_dimension": "VISUAL",
        "severity": "MAJOR",
        "evidence": "frame_0087.png",
        "detector_version": 3,
        "human_decision": "PENDING",
        "disposition": "PENDING",
        "confidence": 0.72,
    }
    payload.update(overrides)
    return payload


def _open_body(
    *,
    project_id: str,
    asset_version_id: str,
    qc_report_ref: str,
    findings: list[dict[str, Any]] | None = None,
    reviewers: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "project_id": project_id,
        "asset_version_id": asset_version_id,
        "qc_report_ref": qc_report_ref,
        "reference_set": {"style_pack": "sp_001"},
        "reviewers": reviewers or ["qc_reviewer", "director_001"],
        "findings": findings if findings is not None else [_finding("i1"), _finding("i2")],
    }


async def _open_session(
    client: AsyncClient,
    admin_engine: AsyncEngine,
    *,
    project_id: str | None = None,
) -> tuple[str, str, str]:
    """开启一次会话，返回 ``(session_id, project_id, asset_version_id)``。"""
    project = project_id or f"proj_qc_{uuid.uuid4().hex[:8]}"
    asset_version_id, report_id = await _seed_report(
        admin_engine, project_id=project
    )
    resp = await client.post(
        "/qc-review-sessions",
        headers=_headers(),
        json=_open_body(
            project_id=project,
            asset_version_id=asset_version_id,
            qc_report_ref=report_id,
        ),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["session_id"], project, asset_version_id


# ----------------------------------------------------------------- 开启会话
async def test_open_session_registers_findings_and_emits_event(
    auth_client: AsyncClient,
    admin_engine: AsyncEngine,
    qc_sm: async_sessionmaker[AsyncSession],
) -> None:
    """缺陷九项落库、检测器多余字段保留、审计事件同事务写入。"""
    session_id, project, asset_version_id = await _open_session(
        auth_client, admin_engine
    )

    async with qc_sm() as session:
        await set_raw_rls_context(session, tenant_id=str(TEST_TENANT_ID))
        row = (
            await session.execute(
                sa.text(
                    "SELECT payload FROM outbox.outbox_events "
                    "WHERE event_type = 'QC_REVIEW_OPENED' "
                    "AND payload->>'session_id' = :sid"
                ),
                {"sid": session_id},
            )
        ).scalar_one_or_none()
    assert row is not None
    assert row["project_id"] == project
    assert row["asset_version_id"] == asset_version_id
    assert row["issue_count"] == 2
    assert row["status"] == "OPEN"
    assert row["created_by"] == "qc_reviewer"


async def test_response_keeps_detector_extra_fields(
    auth_client: AsyncClient, admin_engine: AsyncEngine
) -> None:
    """21.1 只要求九项必填，不要求裁掉检测器多给的数据。"""
    project = f"proj_qc_{uuid.uuid4().hex[:8]}"
    asset_version_id, report_id = await _seed_report(
        admin_engine, project_id=project
    )
    resp = await auth_client.post(
        "/qc-review-sessions",
        headers=_headers(),
        json=_open_body(
            project_id=project,
            asset_version_id=asset_version_id,
            qc_report_ref=report_id,
            findings=[_finding("i1")],
        ),
    )
    assert resp.status_code == 201, resp.text
    finding = resp.json()["findings"][0]
    assert finding["confidence"] == 0.72
    assert finding["timecode"] == "00:00:03:12"
    assert resp.json()["status"] == "OPEN"


async def test_missing_required_issue_field_is_rejected(
    auth_client: AsyncClient, admin_engine: AsyncEngine
) -> None:
    """21.1：缺 evidence 的缺陷不是可复核缺陷 → 422，且指出是第几条。"""
    project = f"proj_qc_{uuid.uuid4().hex[:8]}"
    asset_version_id, report_id = await _seed_report(
        admin_engine, project_id=project
    )
    broken = _finding("i1")
    broken.pop("evidence")
    resp = await auth_client.post(
        "/qc-review-sessions",
        headers=_headers(),
        json=_open_body(
            project_id=project,
            asset_version_id=asset_version_id,
            qc_report_ref=report_id,
            findings=[_finding("i2"), broken],
        ),
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error_code"] == "VALIDATION_ERROR"
    assert "findings[1]" in resp.text
    assert "evidence" in resp.text


async def test_report_from_other_project_is_rejected(
    auth_client: AsyncClient, admin_engine: AsyncEngine
) -> None:
    """证据不得跨项目漂流：报告属于别的项目 → 422。"""
    other_project = f"proj_qc_other_{uuid.uuid4().hex[:8]}"
    asset_version_id, report_id = await _seed_report(
        admin_engine, project_id=other_project
    )
    resp = await auth_client.post(
        "/qc-review-sessions",
        headers=_headers(),
        json=_open_body(
            project_id=f"proj_qc_{uuid.uuid4().hex[:8]}",
            asset_version_id=asset_version_id,
            qc_report_ref=report_id,
        ),
    )
    assert resp.status_code == 422, resp.text
    assert "不符" in resp.text


async def test_report_version_mismatch_is_rejected(
    auth_client: AsyncClient, admin_engine: AsyncEngine
) -> None:
    """报告绑定的是另一个资产版本 → 422（复核对象必须唯一确定）。"""
    project = f"proj_qc_{uuid.uuid4().hex[:8]}"
    _, report_id = await _seed_report(admin_engine, project_id=project)
    resp = await auth_client.post(
        "/qc-review-sessions",
        headers=_headers(),
        json=_open_body(
            project_id=project,
            asset_version_id="av_does_not_exist",
            qc_report_ref=report_id,
        ),
    )
    assert resp.status_code == 422, resp.text
    assert "资产版本" in resp.text


async def test_ghost_report_is_not_found(
    auth_client: AsyncClient,
) -> None:
    """报告不存在 → 404（不区分"不存在"与"不可见"，避免跨租户探测）。"""
    resp = await auth_client.post(
        "/qc-review-sessions",
        headers=_headers(),
        json=_open_body(
            project_id=f"proj_qc_{uuid.uuid4().hex[:8]}",
            asset_version_id="av_ghost",
            qc_report_ref=f"qcr_ghost_{uuid.uuid4().hex[:8]}",
        ),
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["error_code"] == "NOT_FOUND"


async def test_same_report_open_is_idempotent_and_conflict_is_rejected(
    auth_client: AsyncClient, admin_engine: AsyncEngine
) -> None:
    """同一报告重复开启：内容一致返回同一会话，内容变了则 409。"""
    project = f"proj_qc_{uuid.uuid4().hex[:8]}"
    asset_version_id, report_id = await _seed_report(
        admin_engine, project_id=project
    )
    body = _open_body(
        project_id=project,
        asset_version_id=asset_version_id,
        qc_report_ref=report_id,
    )
    first = await auth_client.post(
        "/qc-review-sessions", headers=_headers(), json=body
    )
    assert first.status_code == 201, first.text
    second = await auth_client.post(
        "/qc-review-sessions", headers=_headers(), json=body
    )
    assert second.status_code == 201, second.text
    assert first.json()["session_id"] == second.json()["session_id"]

    conflict_body = dict(body)
    conflict_body["findings"] = [_finding("i1")]  # 缺陷集变了 = 另一条复核链
    conflict = await auth_client.post(
        "/qc-review-sessions", headers=_headers(), json=conflict_body
    )
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["error_code"] == "IDEMPOTENCY_CONFLICT"


# ----------------------------------------------------------------- 状态推进
async def test_full_chain_reaches_closed_and_jump_is_denied(
    auth_client: AsyncClient, admin_engine: AsyncEngine
) -> None:
    """OPEN→…→CLOSED 全链可走，且 RESOLVED→CLOSED 跳关被 409 拒绝。"""
    session_id, project, _ = await _open_session(auth_client, admin_engine)
    url = f"/qc-review-sessions/{session_id}/decisions"

    async def decide(target: str, decisions: list[dict[str, Any]] | None = None) -> Any:
        return await auth_client.post(
            url,
            headers=_headers(),
            json={
                "project_id": project,
                "target_status": target,
                "decisions": decisions or [],
            },
        )

    begun = await decide("IN_REVIEW")
    assert begun.status_code == 200, begun.text
    assert begun.json()["status"] == "IN_REVIEW"

    decided = await decide(
        "DECISION_PENDING",
        [
            {"issue_id": "i1", "human_decision": "CONFIRM_DEFECT",
             "disposition": "FIX_IN_NEXT_ITERATION"},
            {"issue_id": "i2", "human_decision": "FALSE_POSITIVE",
             "disposition": "NO_ACTION"},
        ],
    )
    assert decided.status_code == 200, decided.text
    assert decided.json()["status"] == "DECISION_PENDING"

    resolved = await decide("RESOLVED")
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["status"] == "RESOLVED"

    # 31.5 裁决 ④：跳关拒绝，且告诉调用方唯一合法下一步。
    jump = await decide("CLOSED")
    assert jump.status_code == 409, jump.text
    assert jump.json()["error_code"] == "STATE_TRANSITION_DENIED"
    assert jump.json()["details"]["allowed_targets"] == ["GATE_ELIGIBLE"]

    eligible = await decide("GATE_ELIGIBLE")
    assert eligible.status_code == 200, eligible.text
    closed = await decide("CLOSED")
    assert closed.status_code == 200, closed.text
    assert closed.json()["status"] == "CLOSED"


async def test_decision_pending_requires_every_issue_decided(
    auth_client: AsyncClient, admin_engine: AsyncEngine
) -> None:
    """只裁决一半就推进到 DECISION_PENDING → 422，并列出未裁决的缺陷。

    注意"缺陷没裁决完"是**输入不完整**（422），而"跳关"是**非法转移**（409）：
    两者混用会让调用方分不清该补数据还是该改调用顺序。
    """
    session_id, project, _ = await _open_session(auth_client, admin_engine)
    url = f"/qc-review-sessions/{session_id}/decisions"
    await auth_client.post(
        url,
        headers=_headers(),
        json={"project_id": project, "target_status": "IN_REVIEW", "decisions": []},
    )
    partial = await auth_client.post(
        url,
        headers=_headers(),
        json={
            "project_id": project,
            "target_status": "DECISION_PENDING",
            "decisions": [
                {"issue_id": "i1", "human_decision": "CONFIRM_DEFECT",
                 "disposition": "FIX_IN_NEXT_ITERATION"}
            ],
        },
    )
    assert partial.status_code == 422, partial.text
    assert partial.json()["error_code"] == "VALIDATION_ERROR"
    assert "i2" in partial.text


async def test_jump_from_in_review_to_resolved_is_denied(
    auth_client: AsyncClient, admin_engine: AsyncEngine
) -> None:
    """IN_REVIEW 直接跳 RESOLVED：未登记边 → 409（不是 422）。"""
    session_id, project, _ = await _open_session(auth_client, admin_engine)
    url = f"/qc-review-sessions/{session_id}/decisions"
    await auth_client.post(
        url,
        headers=_headers(),
        json={"project_id": project, "target_status": "IN_REVIEW", "decisions": []},
    )
    jump = await auth_client.post(
        url,
        headers=_headers(),
        json={"project_id": project, "target_status": "RESOLVED", "decisions": []},
    )
    assert jump.status_code == 409, jump.text
    assert jump.json()["details"]["allowed_targets"] == ["DECISION_PENDING"]


async def test_decisions_are_append_only_and_latest_wins(
    auth_client: AsyncClient, admin_engine: AsyncEngine
) -> None:
    """同一缺陷改判 = 追加一条，历史保留，生效值取最新。"""
    session_id, project, _ = await _open_session(auth_client, admin_engine)
    url = f"/qc-review-sessions/{session_id}/decisions"
    await auth_client.post(
        url,
        headers=_headers(),
        json={"project_id": project, "target_status": "IN_REVIEW", "decisions": []},
    )
    first = await auth_client.post(
        url,
        headers=_headers(),
        json={
            "project_id": project,
            "target_status": "IN_REVIEW",
            "decisions": [
                {"issue_id": "i1", "human_decision": "CONFIRM_DEFECT",
                 "disposition": "FIX_IN_NEXT_ITERATION"}
            ],
        },
    )
    assert first.status_code == 200, first.text
    second = await auth_client.post(
        url,
        headers=_headers(),
        json={
            "project_id": project,
            "target_status": "IN_REVIEW",
            "decisions": [
                {"issue_id": "i1", "human_decision": "FALSE_POSITIVE",
                 "disposition": "NO_ACTION"}
            ],
        },
    )
    assert second.status_code == 200, second.text
    decisions = second.json()["decisions"]
    assert [d["human_decision"] for d in decisions] == [
        "CONFIRM_DEFECT",
        "FALSE_POSITIVE",
    ]
    assert decisions[-1]["decided_by"] == "qc_reviewer"


async def test_decided_by_cannot_be_forged_in_body(
    auth_client: AsyncClient, admin_engine: AsyncEngine
) -> None:
    """身份由令牌决定：请求体塞 decided_by 不生效（与 GateDecision 同口径）。"""
    session_id, project, _ = await _open_session(auth_client, admin_engine)
    url = f"/qc-review-sessions/{session_id}/decisions"
    await auth_client.post(
        url,
        headers=_headers(),
        json={"project_id": project, "target_status": "IN_REVIEW", "decisions": []},
    )
    resp = await auth_client.post(
        url,
        headers=_headers("qc_reviewer"),
        json={
            "project_id": project,
            "target_status": "IN_REVIEW",
            "decisions": [
                {"issue_id": "i1", "human_decision": "FALSE_POSITIVE",
                 "disposition": "NO_ACTION", "decided_by": "someone_else"}
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["decisions"][-1]["decided_by"] == "qc_reviewer"


async def test_red_line_issue_cannot_be_waived(
    auth_client: AsyncClient, admin_engine: AsyncEngine
) -> None:
    """4.3 / 21.1：CRITICAL 缺陷不得以任何理由豁免 → 422。"""
    project = f"proj_qc_{uuid.uuid4().hex[:8]}"
    asset_version_id, report_id = await _seed_report(
        admin_engine, project_id=project
    )
    opened = await auth_client.post(
        "/qc-review-sessions",
        headers=_headers(),
        json=_open_body(
            project_id=project,
            asset_version_id=asset_version_id,
            qc_report_ref=report_id,
            findings=[_finding("i1", severity="CRITICAL", qc_dimension="RIGHTS")],
        ),
    )
    assert opened.status_code == 201, opened.text
    session_id = opened.json()["session_id"]
    url = f"/qc-review-sessions/{session_id}/decisions"
    await auth_client.post(
        url,
        headers=_headers(),
        json={"project_id": project, "target_status": "IN_REVIEW", "decisions": []},
    )
    resp = await auth_client.post(
        url,
        headers=_headers(),
        json={
            "project_id": project,
            "target_status": "IN_REVIEW",
            "decisions": [
                {
                    "issue_id": "i1",
                    "human_decision": "WAIVE_WITH_AUTHORITY",
                    "disposition": "WAIVED",
                    "waiver_authority": "director_001",
                    "waiver_reason": "艺术表达需要",
                }
            ],
        },
    )
    assert resp.status_code == 422, resp.text
    assert "不得以任何理由豁免" in resp.text


async def test_agent_cannot_decide(
    auth_client: AsyncClient, admin_engine: AsyncEngine
) -> None:
    """21.2：AI 只发现与提供证据，不单独裁决（带齐角色也拿不到结论）。"""
    session_id, project, _ = await _open_session(auth_client, admin_engine)
    url = f"/qc-review-sessions/{session_id}/decisions"
    await auth_client.post(
        url,
        headers=_headers(),
        json={"project_id": project, "target_status": "IN_REVIEW", "decisions": []},
    )
    resp = await auth_client.post(
        url,
        headers=_headers("qc_agent", subject_type="agent"),
        json={
            "project_id": project,
            "target_status": "IN_REVIEW",
            "decisions": [
                {"issue_id": "i1", "human_decision": "CONFIRM_DEFECT",
                 "disposition": "FIX_IN_NEXT_ITERATION"}
            ],
        },
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["error_code"] == "AUTH_SCOPE_DENIED"


async def test_unauthorized_role_cannot_open_session(
    auth_client: AsyncClient, admin_engine: AsyncEngine
) -> None:
    """未在契约允许主体内的角色一律拒绝（fail-closed，不静默放行）。"""
    project = f"proj_qc_{uuid.uuid4().hex[:8]}"
    asset_version_id, report_id = await _seed_report(
        admin_engine, project_id=project
    )
    resp = await auth_client.post(
        "/qc-review-sessions",
        headers=_headers("qc_producer", roles=["制片"]),
        json=_open_body(
            project_id=project,
            asset_version_id=asset_version_id,
            qc_report_ref=report_id,
        ),
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["error_code"] == "AUTH_SCOPE_DENIED"


async def test_concurrent_decide_conflicts_on_version(
    auth_client: AsyncClient,
    admin_engine: AsyncEngine,
    qc_sm: async_sessionmaker[AsyncSession],
) -> None:
    """两个复核人基于同一版本各自推进：后写者拿到 CONCURRENT_MODIFICATION。

    刻意不靠"并发发两个 HTTP 请求看谁输"来证明：那取决于事件循环调度，
    在快机器上可能两笔都成功（第一笔已提交后第二笔才读到新版本），用例会
    时红时绿。这里显式制造"同一读版本、两次写"的事实——CAS 的语义就是
    "第二个写者必须失败"，与调度无关。
    """
    session_id, project, _ = await _open_session(auth_client, admin_engine)
    principal = Principal(
        subject_id="qc_reviewer",
        tenant_id=TEST_TENANT_ID,
        organization_id=TEST_TENANT_ID,
        subject_type=SubjectType.HUMAN,
        project_ids=frozenset({"*"}),
    )

    async with qc_sm() as first_session, qc_sm() as second_session:
        await apply_db_context(first_session, project_ids=(project,))
        await apply_db_context(second_session, project_ids=(project,))
        first_repo = SqlAlchemyQCReviewSessionRepository(first_session)
        second_repo = SqlAlchemyQCReviewSessionRepository(second_session)

        first_view = await first_repo.get_by_id(session_id)
        second_view = await second_repo.get_by_id(session_id)
        assert first_view is not None and second_view is not None
        read_version = second_view.entity_version

        first_view.start_review(principal=principal)
        await first_repo.update(first_view, expected_version=read_version)
        await first_session.commit()

        second_view.start_review(principal=principal)
        with pytest.raises(DomainError) as exc_info:
            await second_repo.update(second_view, expected_version=read_version)
    assert exc_info.value.error_code is ErrorRegistry.CONCURRENT_MODIFICATION


# --------------------------------------------------------- 数据库层独立兜底
def _json(payload: Any) -> str:
    """把 Python 载荷序列化成 JSONB 字面量（种子插入用）。"""
    return json.dumps(payload, ensure_ascii=False)


async def _insert_session(
    admin_engine: AsyncEngine,
    *,
    project_id: str,
    status: str = "RESOLVED",
    asset_version_id: str = "av_guard",
) -> str:
    """直插一条指定状态的会话（超级用户；绕过所有应用层校验）。"""
    session_id = f"qcs_guard_{uuid.uuid4().hex[:12]}"
    finding = _finding("i1")
    async with admin_engine.begin() as conn:
        await conn.execute(
            sa.text(
                """
                INSERT INTO qc_governance.qc_review_sessions (
                    entity_id, entity_type, schema_version, entity_version,
                    status, owner_module, tenant_id, organization_id,
                    created_at, created_by, updated_at, audit_ref,
                    project_id, asset_version_id, qc_report_ref,
                    reference_set, reviewers, findings, decisions
                ) VALUES (
                    :sid, 'QCReviewSession', 205, 1,
                    :status, 'qc_governance', :tid, :tid,
                    now(), 'guard_seed', now(), NULL,
                    :pid, :aid, :rid,
                    '{}'::jsonb, '["qc_reviewer"]'::jsonb,
                    CAST(:findings AS jsonb), '[]'::jsonb
                )
                """
            ),
            {
                "sid": session_id,
                "status": status,
                "tid": TEST_TENANT_ID,
                "pid": project_id,
                "aid": asset_version_id,
                "rid": f"qcr_guard_{uuid.uuid4().hex[:8]}",
                "findings": _json([finding]),
            },
        )
    return session_id


async def test_db_guard_blocks_unregistered_transition(
    admin_engine: AsyncEngine,
) -> None:
    """ADR-R20：RESOLVED→CLOSED 直连 SQL 被拒绝（不经过任何应用代码）。"""
    project = f"proj_guard_{uuid.uuid4().hex[:8]}"
    session_id = await _insert_session(admin_engine, project_id=project)
    with pytest.raises(DBAPIError) as exc_info:
        async with admin_engine.begin() as conn:
            await conn.execute(
                sa.text(
                    "UPDATE qc_governance.qc_review_sessions "
                    "SET status = 'CLOSED' WHERE entity_id = :sid"
                ),
                {"sid": session_id},
            )
    assert "非法状态转移" in str(exc_info.value)


async def test_db_guard_allows_registered_transition(
    admin_engine: AsyncEngine,
) -> None:
    """已登记边仍然走得通——否则"拦住了"可以靠整列冻结伪造。"""
    project = f"proj_guard_{uuid.uuid4().hex[:8]}"
    session_id = await _insert_session(admin_engine, project_id=project)
    async with admin_engine.begin() as conn:
        await conn.execute(
            sa.text(
                "UPDATE qc_governance.qc_review_sessions "
                "SET status = 'GATE_ELIGIBLE' WHERE entity_id = :sid"
            ),
            {"sid": session_id},
        )
        status = (
            await conn.execute(
                sa.text(
                    "SELECT status FROM qc_governance.qc_review_sessions "
                    "WHERE entity_id = :sid"
                ),
                {"sid": session_id},
            )
        ).scalar_one()
    assert status == "GATE_ELIGIBLE"


async def test_db_guard_blocks_findings_rewrite(admin_engine: AsyncEngine) -> None:
    """复查输入不可事后改写：改 findings 等于把"复核了什么"改写掉。"""
    project = f"proj_guard_{uuid.uuid4().hex[:8]}"
    session_id = await _insert_session(admin_engine, project_id=project)
    with pytest.raises(DBAPIError) as exc_info:
        async with admin_engine.begin() as conn:
            await conn.execute(
                sa.text(
                    "UPDATE qc_governance.qc_review_sessions "
                    "SET findings = '[]'::jsonb WHERE entity_id = :sid"
                ),
                {"sid": session_id},
            )
    assert "不可变内容列" in str(exc_info.value)


async def test_db_guard_blocks_delete(admin_engine: AsyncEngine) -> None:
    """删会话等于抹掉证据链：DELETE 一律 RAISE。"""
    project = f"proj_guard_{uuid.uuid4().hex[:8]}"
    session_id = await _insert_session(admin_engine, project_id=project)
    with pytest.raises(DBAPIError) as exc_info:
        async with admin_engine.begin() as conn:
            await conn.execute(
                sa.text(
                    "DELETE FROM qc_governance.qc_review_sessions "
                    "WHERE entity_id = :sid"
                ),
                {"sid": session_id},
            )
    assert "禁止物理删除" in str(exc_info.value)


async def test_db_rejects_unregistered_status_on_insert(
    admin_engine: AsyncEngine,
) -> None:
    """契约外的状态值写不进去（INSERT 由 CHECK 约束拦）。

    为什么不测 ``UPDATE ... SET status='ARCHIVED'``：BEFORE UPDATE 触发器先跑，
    报的是"非法状态转移"，CHECK 根本轮不到——用 UPDATE 测 CHECK 会得到一条
    看起来通过、实际测的是另一层的断言。CHECK 的真实防线是"建档时就不能有
    契约外状态"，因此用 INSERT 测。
    """
    project = f"proj_guard_{uuid.uuid4().hex[:8]}"
    with pytest.raises(DBAPIError) as exc_info:
        async with admin_engine.begin() as conn:
            await conn.execute(
                sa.text(
                    "INSERT INTO qc_governance.qc_review_sessions ("
                    "entity_id, entity_type, schema_version, entity_version,"
                    " status, owner_module, tenant_id, organization_id,"
                    " created_at, created_by, updated_at, audit_ref,"
                    " project_id, asset_version_id, qc_report_ref,"
                    " reference_set, reviewers, findings, decisions"
                    ") VALUES ("
                    ":sid, 'QCReviewSession', 205, 1,"
                    " 'ARCHIVED', 'qc_governance', :tid, :tid,"
                    " now(), 'guard_seed', now(), NULL,"
                    " :pid, 'av_guard', 'qcr_guard',"
                    " '{}'::jsonb, '[\"qc_reviewer\"]'::jsonb,"
                    " CAST(:findings AS jsonb), '[]'::jsonb"
                    ")"
                ),
                {
                    "sid": f"qcs_bad_{uuid.uuid4().hex[:12]}",
                    "tid": TEST_TENANT_ID,
                    "pid": project,
                    "findings": _json([_finding("i1")]),
                },
            )
    assert "ck_qc_governance_qc_review_sessions_status_registered" in str(
        exc_info.value
    )


async def test_db_rejects_empty_findings_on_insert(
    admin_engine: AsyncEngine,
) -> None:
    """空会话不得存在（CHECK 兜住直连 SQL 的空缺陷写入）。"""
    project = f"proj_guard_{uuid.uuid4().hex[:8]}"
    with pytest.raises(DBAPIError) as exc_info:
        async with admin_engine.begin() as conn:
            await conn.execute(
                sa.text(
                    "INSERT INTO qc_governance.qc_review_sessions ("
                    "entity_id, entity_type, schema_version, entity_version,"
                    " status, owner_module, tenant_id, organization_id,"
                    " created_at, created_by, updated_at, audit_ref,"
                    " project_id, asset_version_id, qc_report_ref,"
                    " reference_set, reviewers, findings, decisions"
                    ") VALUES ("
                    ":sid, 'QCReviewSession', 205, 1,"
                    " 'OPEN', 'qc_governance', :tid, :tid,"
                    " now(), 'guard_seed', now(), NULL,"
                    " :pid, 'av_guard', 'qcr_guard',"
                    " '{}'::jsonb, '[\"qc_reviewer\"]'::jsonb,"
                    " '[]'::jsonb, '[]'::jsonb"
                    ")"
                ),
                {
                    "sid": f"qcs_empty_{uuid.uuid4().hex[:12]}",
                    "tid": TEST_TENANT_ID,
                    "pid": project,
                },
            )
    assert "findings_not_empty" in str(exc_info.value)
