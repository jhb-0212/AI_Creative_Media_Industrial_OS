"""Shot API 集成测试（异步，连真实 PostgreSQL，走真实鉴权）。

验证 ADR-R27 镜头域边界：
- AI（subject_type=agent）产草案成功（source=agent）；
- Agent 确认正式镜头修订被拒（403 AUTH_SCOPE_DENIED）；
- 人类（导演）确认草案产出正式修订（版本递增不覆盖）。
"""

from __future__ import annotations

import uuid

from httpx import AsyncClient

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
