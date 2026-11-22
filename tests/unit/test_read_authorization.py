"""读接口授权测试（A：资源读取接入 RBAC，闭合读写边界）。

覆盖：
- GET /projects/{id}、GET /episodes/{id}、GET /scripts/{id} 在角色不匹配时
  返回 403 AUTH_SCOPE_DENIED（负向，真实调接口）。
- GET /projects/{id} 在角色命中时返回 200（正向，证明授权放行）。
- 无 Bearer 凭证时返回 401（认证先于授权）。
"""

from __future__ import annotations

import uuid

from httpx import ASGITransport, AsyncClient

from apps.api.main import app
from tests.conftest import make_test_token


def _client(roles: list[str], subject_id: str = "read_user") -> AsyncClient:
    """构造带指定角色 token 的 client（真实 JWT + 真实 ASGI 端点）。"""
    transport = ASGITransport(app=app)
    token = make_test_token(subject_id=subject_id, roles=roles)
    headers = {"Authorization": f"Bearer {token}"}
    return AsyncClient(transport=transport, base_url="http://test", headers=headers)


async def test_project_read_forbidden_for_unknown_role() -> None:
    """PROJECT.READ 未包含「无关角色」→ 403。"""
    async with _client(["无关角色"]) as c:
        resp = await c.get("/projects/proj_any")
    assert resp.status_code == 403
    assert resp.json()["error_code"] == "AUTH_SCOPE_DENIED"


async def test_episode_read_forbidden_for_unknown_role() -> None:
    """EPISODE.READ 未包含「无关角色」→ 403。"""
    async with _client(["无关角色"]) as c:
        resp = await c.get("/episodes/ep_any")
    assert resp.status_code == 403
    assert resp.json()["error_code"] == "AUTH_SCOPE_DENIED"


async def test_script_read_forbidden_for_unknown_role() -> None:
    """SCRIPT.READ 未包含「无关角色」→ 403。"""
    async with _client(["无关角色"]) as c:
        resp = await c.get("/scripts/scr_any")
    assert resp.status_code == 403
    assert resp.json()["error_code"] == "AUTH_SCOPE_DENIED"


async def test_project_read_allowed_for_producer() -> None:
    """制片在 PROJECT.READ 允许集合内：建项目后可正常读取（200）。"""
    entity_id = f"proj_read_{uuid.uuid4().hex[:8]}"
    async with _client(["制片"], subject_id="producer_read") as c:
        created = await c.post(
            "/projects",
            json={
                "project_name": "读取授权项目",
                "project_tier": "B",
                "entity_id": entity_id,
            },
        )
        assert created.status_code == 201
        got = await c.get(f"/projects/{entity_id}")
    assert got.status_code == 200


async def test_read_requires_authentication() -> None:
    """未携带 Bearer 凭证 → 401（认证先于授权）。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.get("/projects/proj_any")
    assert resp.status_code == 401
