"""Shot API 集成测试（异步，连真实 PostgreSQL，走真实鉴权）。

验证 ADR-R27 镜头域边界：
- AI（subject_type=agent）产草案成功（source=agent）；
- Agent 确认正式镜头修订被拒（403 AUTH_SCOPE_DENIED）；
- 人类（导演）确认草案产出正式修订（版本递增不覆盖）。
"""

from __future__ import annotations

import json
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.conftest import make_test_headers


def _auth_headers(
    subject_id: str, *, subject_type: str = "human", roles: list[str] | None = None
) -> dict[str, str]:
    return make_test_headers(
        subject_id=subject_id, subject_type=subject_type, roles=roles
    )


async def test_agent_creates_shot_draft(auth_client: AsyncClient) -> None:
    """AI（agent 主体）产拆镜草案成功，source=agent。"""
    project_id = f"proj_{uuid.uuid4().hex[:8]}"
    await auth_client.post(
        "/projects",
        json={"project_name": f"项目_{project_id}", "project_tier": "B", "entity_id": project_id},
    )

    headers = _auth_headers("agent_001", subject_type="agent", roles=["编剧"])
    resp = await auth_client.post(
        "/shots/drafts",
        headers=headers,
        json={
            "project_id": project_id,
            "episode_ref": "EP001",
            "scene_ref": "SC001",
            "shot_code": "SHOT_001",
            "intent_summary": "角色 A 情绪转折",
            "shot_description": "中景，角色 A 转身凝视",
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["entity_type"] == "ShotDraft"
    assert body["status"] == "DRAFT"
    assert body["source"] == "agent"
    assert body["created_by"] == "agent_001"


async def test_human_creates_shot_draft_source_human(auth_client: AsyncClient) -> None:
    """人类主体产草案 source=human。"""
    project_id = f"proj_{uuid.uuid4().hex[:8]}"
    await auth_client.post(
        "/projects",
        json={"project_name": f"项目_{project_id}", "project_tier": "B", "entity_id": project_id},
    )

    headers = _auth_headers("writer_001", subject_type="human", roles=["编剧"])
    resp = await auth_client.post(
        "/shots/drafts",
        headers=headers,
        json={
            "project_id": project_id,
            "episode_ref": "EP001",
            "scene_ref": "SC001",
            "shot_code": "SHOT_001",
            "intent_summary": "意图",
            "shot_description": "描述",
        },
    )
    assert resp.status_code == 201
    assert resp.json()["source"] == "human"


async def test_human_confirms_revision(auth_client: AsyncClient) -> None:
    """人类导演确认草案 → 产出正式 ShotRevision。"""
    project_id = f"proj_{uuid.uuid4().hex[:8]}"
    await auth_client.post(
        "/projects",
        json={"project_name": f"项目_{project_id}", "project_tier": "B", "entity_id": project_id},
    )

    # AI 先产草案
    agent_headers = _auth_headers("agent_001", subject_type="agent", roles=["编剧"])
    draft_resp = await auth_client.post(
        "/shots/drafts",
        headers=agent_headers,
        json={
            "project_id": project_id,
            "episode_ref": "EP001",
            "scene_ref": "SC001",
            "shot_code": "SHOT_001",
            "intent_summary": "意图",
            "shot_description": "描述",
        },
    )
    assert draft_resp.status_code == 201
    draft_id = draft_resp.json()["entity_id"]

    # 人类导演确认
    director_headers = _auth_headers("director_001", subject_type="human", roles=["导演"])
    resp = await auth_client.post(
        "/shots/revisions",
        headers=director_headers,
        json={"draft_id": draft_id, "shot_id": f"shot_{uuid.uuid4().hex[:8]}", "project_id": project_id},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["entity_type"] == "ShotRevision"
    assert body["revision"] == 1
    assert body["created_by"] == "director_001"


@pytest.mark.agent_negative("AGENT-NEG-002")
async def test_agent_cannot_confirm_revision(auth_client: AsyncClient) -> None:
    """Agent 确认正式镜头修订 → 403 AUTH_SCOPE_DENIED（「Agent 不得自批自用」）。"""
    project_id = f"proj_{uuid.uuid4().hex[:8]}"
    await auth_client.post(
        "/projects",
        json={"project_name": f"项目_{project_id}", "project_tier": "B", "entity_id": project_id},
    )

    agent_headers = _auth_headers("agent_001", subject_type="agent", roles=["导演"])
    draft_resp = await auth_client.post(
        "/shots/drafts",
        headers=agent_headers,
        json={
            "project_id": project_id,
            "episode_ref": "EP001",
            "scene_ref": "SC001",
            "shot_code": "SHOT_001",
            "intent_summary": "意图",
            "shot_description": "描述",
        },
    )
    assert draft_resp.status_code == 201
    draft_id = draft_resp.json()["entity_id"]

    resp = await auth_client.post(
        "/shots/revisions",
        headers=agent_headers,  # Agent 确认 → 拒绝
        json={"draft_id": draft_id, "shot_id": f"shot_{uuid.uuid4().hex[:8]}", "project_id": project_id},
    )
    assert resp.status_code == 403
    assert resp.json()["error_code"] == "AUTH_SCOPE_DENIED"


async def test_confirm_revision_increments_version(auth_client: AsyncClient) -> None:
    """同 shot 二次确认 → 版本递增，旧修订标 SUPERSEDED。"""
    project_id = f"proj_{uuid.uuid4().hex[:8]}"
    await auth_client.post(
        "/projects",
        json={"project_name": f"项目_{project_id}", "project_tier": "B", "entity_id": project_id},
    )
    shot_id = f"shot_{uuid.uuid4().hex[:8]}"
    agent_headers = _auth_headers("agent_001", subject_type="agent", roles=["编剧"])
    director_headers = _auth_headers("director_001", subject_type="human", roles=["导演"])

    async def _draft(desc: str) -> str:
        r = await auth_client.post(
            "/shots/drafts",
            headers=agent_headers,
            json={
                "project_id": project_id,
                "episode_ref": "EP001",
                "scene_ref": "SC001",
                "shot_code": "SHOT_001",
                "intent_summary": "意图",
                "shot_description": desc,
            },
        )
        assert r.status_code == 201
        return r.json()["entity_id"]

    # 第一次确认
    d1 = await _draft("描述 v1")
    r1 = await auth_client.post(
        "/shots/revisions", headers=director_headers,
        json={"draft_id": d1, "shot_id": shot_id, "project_id": project_id},
    )
    assert r1.status_code == 201
    rev1_id = r1.json()["entity_id"]
    assert r1.json()["revision"] == 1

    # 第二次确认（同 shot）
    d2 = await _draft("描述 v2")
    r2 = await auth_client.post(
        "/shots/revisions", headers=director_headers,
        json={"draft_id": d2, "shot_id": shot_id, "project_id": project_id},
    )
    assert r2.status_code == 201
    assert r2.json()["revision"] == 2

    # 旧修订保留（append-only 不覆盖）：两条修订都在，revision 号 1 和 2。
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from packages.shared.database import build_migration_url

    engine = create_async_engine(build_migration_url())
    try:
        async with engine.connect() as conn:
            rev1_revision = await conn.scalar(
                text(
                    "SELECT revision FROM shot_planning.shot_revisions "
                    "WHERE entity_id = :e"
                ),
                {"e": rev1_id},
            )
    finally:
        await engine.dispose()
    assert rev1_revision == 1


# --------------------------------------------------------------------------- #
# ADR-R27 阶段 1C：ShotSpec 锁定 / ConstraintPack 创建与激活
# --------------------------------------------------------------------------- #


async def _seed_shot_spec(
    shot_sm: async_sessionmaker[AsyncSession],
) -> tuple[str, str]:
    """种子路径：直接经 shot_planning 域仓储建立一条规格（无冻结创建动作）。"""
    from services.shot_planning.domain.shot_spec import ShotSpec
    from services.shot_planning.repository.shot_spec_sqlalchemy_repo import (
        SqlAlchemyShotSpecRepository,
    )
    from tests.conftest import TEST_TENANT_ID, apply_db_context

    spec = ShotSpec(
        entity_id=uuid.uuid4().hex,
        shot_revision_id=uuid.uuid4().hex,
        shot_id=f"shot_{uuid.uuid4().hex[:8]}",
        project_id=f"proj_{uuid.uuid4().hex[:8]}",
        duration_ms=4200,
        aspect_ratio="16:9",
        fps="24/1",
        coordinate_system="RIGHT_HANDED/Y/METER",
        camera_path={"type": "DOLLY", "keyframes": []},
        created_by="director_001",
        tenant_id=TEST_TENANT_ID,
    )
    async with shot_sm() as session:
        await apply_db_context(session, project_ids=(spec.project_id,))
        await SqlAlchemyShotSpecRepository(session).add(spec)
        await session.commit()
    return spec.entity_id, spec.project_id


async def test_director_locks_shot_spec(
    auth_client: AsyncClient,
    shot_sm: async_sessionmaker[AsyncSession],
) -> None:
    """导演锁定规格维度：锁定维度/依据/版本齐备，返回锁定后的规格。"""
    spec_id, project_id = await _seed_shot_spec(shot_sm)
    headers = _auth_headers("director_001", subject_type="human", roles=["导演"])
    resp = await auth_client.post(
        f"/shots/specs/{spec_id}/lock",
        headers=headers,
        json={
            "project_id": project_id,
            "locked_dimensions": ["camera_path", "cut_points"],
            "rationale": "运镜与切点经导演确认",
            "version": 1,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["entity_type"] == "ShotSpec"
    assert body["shot_spec_id"] == body["entity_id"]
    # 契约口径：ShotSpec 结构化字段以 JSON 文本序列化。
    assert json.loads(body["locked_dimensions"]) == ["camera_path", "cut_points"]
    assert body["duration_ms"] == "4200"
    assert body["entity_version"] == 2


async def test_agent_cannot_lock_shot_spec(
    auth_client: AsyncClient,
    shot_sm: async_sessionmaker[AsyncSession],
) -> None:
    """Agent 锁定规格 → 403（仅导演/授权 TD）。"""
    spec_id, project_id = await _seed_shot_spec(shot_sm)
    headers = _auth_headers("agent_001", subject_type="agent", roles=["导演"])
    resp = await auth_client.post(
        f"/shots/specs/{spec_id}/lock",
        headers=headers,
        json={
            "project_id": project_id,
            "locked_dimensions": ["camera_path"],
            "rationale": "越权锁定",
            "version": 1,
        },
    )
    assert resp.status_code == 403
    assert resp.json()["error_code"] == "AUTH_SCOPE_DENIED"


async def test_lock_shot_spec_unknown_dimension_denied(
    auth_client: AsyncClient,
    shot_sm: async_sessionmaker[AsyncSession],
) -> None:
    """锁定 7.9 维度词典之外的维度 → 422（结构化 VALIDATION_ERROR）。"""
    spec_id, project_id = await _seed_shot_spec(shot_sm)
    headers = _auth_headers("director_001", subject_type="human", roles=["导演"])
    resp = await auth_client.post(
        f"/shots/specs/{spec_id}/lock",
        headers=headers,
        json={
            "project_id": project_id,
            "locked_dimensions": ["makeup"],
            "rationale": "未登记维度",
            "version": 1,
        },
    )
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "VALIDATION_ERROR"


async def test_agent_creates_and_director_locks_constraint_pack(
    auth_client: AsyncClient,
) -> None:
    """C3：Agent 创建草案 → 导演激活（DRAFT→ACTIVE，审批人固化）。"""
    project_id = f"proj_{uuid.uuid4().hex[:8]}"
    await auth_client.post(
        "/projects",
        json={"project_name": f"项目_{project_id}", "project_tier": "B", "entity_id": project_id},
    )
    agent_headers = _auth_headers("agent_001", subject_type="agent", roles=["TD"])
    create_resp = await auth_client.post(
        "/shots/constraint-packs",
        headers=agent_headers,
        json={
            "project_id": project_id,
            "shot_ids": [f"shot_{uuid.uuid4().hex[:8]}"],
            "applicability": "APPLICABLE",
            "locked_dimensions": ["camera_path"],
            "constraints": [
                {
                    "constraint_id": "c_001",
                    "target": "char_001",
                    "shot_scope": "SHOT_001..SHOT_003",
                    "rule": "服装状态连续",
                    "source": "SCRIPT",
                    "authority_level": "HARD",
                    "valid_at": "2026-11-17T00:00:00Z",
                    "approval_ref": "approval_001",
                }
            ],
        },
    )
    assert create_resp.status_code == 201, create_resp.text
    body = create_resp.json()
    assert body["entity_type"] == "ConstraintPack"
    assert body["status"] == "DRAFT"
    # 契约必填 string：未审批的 DRAFT 以空串表达「尚未发生」。
    assert body["approved_by"] == ""
    assert body["approved_at"] == ""

    pack_id = body["entity_id"]
    director_headers = _auth_headers("director_001", subject_type="human", roles=["导演"])
    lock_resp = await auth_client.post(
        f"/shots/constraint-packs/{pack_id}/lock",
        headers=director_headers,
        json={"project_id": project_id},
    )
    assert lock_resp.status_code == 200, lock_resp.text
    locked = lock_resp.json()
    assert locked["status"] == "ACTIVE"
    assert locked["approved_by"] == "director_001"
    assert locked["approved_at"]


async def test_agent_cannot_lock_constraint_pack(auth_client: AsyncClient) -> None:
    """Agent 激活约束包 → 403（不得自批自用）。"""
    project_id = f"proj_{uuid.uuid4().hex[:8]}"
    await auth_client.post(
        "/projects",
        json={"project_name": f"项目_{project_id}", "project_tier": "B", "entity_id": project_id},
    )
    agent_headers = _auth_headers("agent_001", subject_type="agent", roles=["TD"])
    create_resp = await auth_client.post(
        "/shots/constraint-packs",
        headers=agent_headers,
        json={
            "project_id": project_id,
            "shot_ids": [f"shot_{uuid.uuid4().hex[:8]}"],
            "applicability": "NOT_APPLICABLE",
        },
    )
    assert create_resp.status_code == 201
    pack_id = create_resp.json()["entity_id"]

    lock_resp = await auth_client.post(
        f"/shots/constraint-packs/{pack_id}/lock",
        headers=agent_headers,
        json={"project_id": project_id},
    )
    assert lock_resp.status_code == 403
    assert lock_resp.json()["error_code"] == "AUTH_SCOPE_DENIED"


async def test_constraint_pack_not_applicable_flow(auth_client: AsyncClient) -> None:
    """C3：NOT_APPLICABLE 包同样须经 ACTIVE（不得作为默认值绕过）。"""
    project_id = f"proj_{uuid.uuid4().hex[:8]}"
    await auth_client.post(
        "/projects",
        json={"project_name": f"项目_{project_id}", "project_tier": "B", "entity_id": project_id},
    )
    director_headers = _auth_headers("director_001", subject_type="human", roles=["导演"])
    create_resp = await auth_client.post(
        "/shots/constraint-packs",
        headers=director_headers,
        json={
            "project_id": project_id,
            "shot_ids": [f"shot_{uuid.uuid4().hex[:8]}"],
            "applicability": "NOT_APPLICABLE",
        },
    )
    assert create_resp.status_code == 201
    body = create_resp.json()
    assert body["applicability"] == "NOT_APPLICABLE"
    assert body["locked_dimensions"] == []

    lock_resp = await auth_client.post(
        f"/shots/constraint-packs/{body['entity_id']}/lock",
        headers=director_headers,
        json={"project_id": project_id},
    )
    assert lock_resp.status_code == 200
    assert lock_resp.json()["status"] == "ACTIVE"
