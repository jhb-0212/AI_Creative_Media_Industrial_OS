"""运行时 RBAC 集成测试（P0-3 收口 + R6/R9：安全必须真实执行）。

核心验证：``action_registry.allowed_principals`` 已接到 FastAPI 依赖层——
- 角色不在 allowed_principals → 403 AUTH_SCOPE_DENIED（负向，真实调接口）。
- 角色命中 allowed_principals → 正常通过（正向，真实调接口）。
- 认证仍先于授权（无 token 走 401，见 test_identity_auth.py）。

覆盖当前实现的 5 个写入口：
PROJECT.CREATE / EPISODE.CREATE / SCRIPT.IMPORT / SCRIPT.LOCK.REQUEST /
SCRIPT.LOCK.APPROVE。
"""

from __future__ import annotations

import uuid

from httpx import ASGITransport, AsyncClient

from apps.api.main import app
from tests.conftest import make_test_token


def _client_with_roles(roles: list[str], subject_id: str = "rbac_user") -> AsyncClient:
    """构造带指定角色 token 的 client（真实 JWT + 真实 ASGI 端点）。"""
    transport = ASGITransport(app=app)
    token = make_test_token(subject_id=subject_id, roles=roles)
    headers = {"Authorization": f"Bearer {token}"}
    return AsyncClient(transport=transport, base_url="http://test", headers=headers)


async def test_project_create_forbidden_for_writer_role() -> None:
    """PROJECT.CREATE 仅限 项目管理员/制片；编剧越权 → 403。"""
    async with _client_with_roles(["编剧"]) as c:
        resp = await c.post(
            "/projects",
            json={"project_name": "越权项目", "project_tier": "B"},
        )
    assert resp.status_code == 403
    assert resp.json()["error_code"] == "AUTH_SCOPE_DENIED"


async def test_episode_create_forbidden_for_qc_role() -> None:
    """EPISODE.CREATE 仅限 导演/编剧/制片；QC 越权 → 403。"""
    async with _client_with_roles(["QC 审核人"]) as c:
        resp = await c.post(
            "/episodes",
            json={"project_id": "proj_x", "episode_ref": "EP001", "title": "越权集数"},
        )
    assert resp.status_code == 403
    assert resp.json()["error_code"] == "AUTH_SCOPE_DENIED"


async def test_script_import_forbidden_for_qc_role() -> None:
    """SCRIPT.IMPORT 仅限 导演/编剧/制片；QC 越权 → 403。"""
    async with _client_with_roles(["QC 审核人"]) as c:
        resp = await c.post(
            "/scripts/import",
            json={"project_id": "proj_x", "episode_ref": "EP001", "title": "越权剧本"},
        )
    assert resp.status_code == 403
    assert resp.json()["error_code"] == "AUTH_SCOPE_DENIED"


async def test_lock_request_forbidden_for_producer_role() -> None:
    """SCRIPT.LOCK.REQUEST 仅限 导演/编剧；制片只读、越权申请 → 403。"""
    async with _client_with_roles(["制片"]) as c:
        resp = await c.post("/scripts/scr_x/lock/request")
    assert resp.status_code == 403
    assert resp.json()["error_code"] == "AUTH_SCOPE_DENIED"


async def test_lock_approve_forbidden_for_writer_role() -> None:
    """编剧不具备 SCRIPT.LOCK.APPROVE 的角色权限 → 角色级 403。

    注意：这是「角色不在 allowed_principals」的授权断言，不是职责分离断言；
    申请人≠批准人的运行时比对见 test_script_api 的自批拒绝用例。
    """
    async with _client_with_roles(["编剧"]) as c:
        resp = await c.post("/scripts/scr_x/lock/approve")
    assert resp.status_code == 403
    assert resp.json()["error_code"] == "AUTH_SCOPE_DENIED"


async def test_project_create_allowed_for_producer_role() -> None:
    """PROJECT.CREATE 允许制片 → 201（证明授权命中时放行）。"""
    eid = f"proj_rbac_{uuid.uuid4().hex[:8]}"
    async with _client_with_roles(["制片"], subject_id="producer_rbac") as c:
        resp = await c.post(
            "/projects",
            json={"project_name": "合法项目", "project_tier": "B", "entity_id": eid},
        )
    assert resp.status_code == 201


async def test_rbac_allows_authorized_roles_across_write_flow() -> None:
    """正向全链路：各写入口在角色命中时放行，且职责分离仍生效。

    - 制片：建项目（PROJECT.CREATE）+ 登记集数（EPISODE.CREATE）。
    - 编剧：导入剧本（SCRIPT.IMPORT）+ 申请锁定（SCRIPT.LOCK.REQUEST）。
    - 导演：批准锁定（SCRIPT.LOCK.APPROVE）→ LOCKED。
    """
    project_id = f"proj_rbac_flow_{uuid.uuid4().hex[:8]}"

    async with _client_with_roles(["制片"], subject_id="producer_rbac") as producer:
        rp = await producer.post(
            "/projects",
            json={"project_name": "全链路项目", "project_tier": "B", "entity_id": project_id},
        )
        assert rp.status_code == 201
        re_ = await producer.post(
            "/episodes",
            json={"project_id": project_id, "episode_ref": "EP001", "title": "集数_EP001"},
        )
        assert re_.status_code == 201

    script_id: str | None = None
    async with _client_with_roles(["编剧"], subject_id="writer_rbac") as writer:
        imp = await writer.post(
            "/scripts/import",
            json={
                "project_id": project_id,
                "episode_ref": "EP001",
                "title": "全链路剧本",
                "entity_id": f"scr_rbac_flow_{uuid.uuid4().hex[:8]}",
            },
        )
        assert imp.status_code == 201
        script_id = imp.json()["entity_id"]
        req = await writer.post(f"/scripts/{script_id}/lock/request")
        assert req.status_code == 200

    async with _client_with_roles(["导演"], subject_id="director_rbac") as director:
        appr = await director.post(f"/scripts/{script_id}/lock/approve")
        assert appr.status_code == 200
        assert appr.json()["status"] == "LOCKED"


async def test_unknown_role_denied_on_all_write_endpoints() -> None:
    """QC 不在任一写动作的 allowed_principals → 5 个写入口全部 403。

    这条用例专门防「默认 token 授予多角色、端点删掉 require_role 也没人发现」：
    角色不命中时必须整条链路都拒绝，而不是靠某个用例恰好没覆盖到。
    """
    project_id = f"proj_deny_{uuid.uuid4().hex[:8]}"

    async with _client_with_roles(["QC"]) as c:
        responses = [
            await c.post(
                "/projects",
                json={"project_name": "越权项目", "project_tier": "B", "entity_id": project_id},
            ),
            await c.post(
                "/episodes",
                json={"project_id": project_id, "episode_ref": "EP001", "title": "越权集数"},
            ),
            await c.post(
                "/scripts/import",
                json={
                    "project_id": project_id,
                    "episode_ref": "EP001",
                    "title": "越权剧本",
                    "entity_id": f"scr_deny_{uuid.uuid4().hex[:8]}",
                },
            ),
            await c.post("/scripts/scr_deny/lock/request"),
            await c.post("/scripts/scr_deny/lock/approve"),
        ]

    assert [r.status_code for r in responses] == [403] * 5
    assert all(r.json()["error_code"] == "AUTH_SCOPE_DENIED" for r in responses)
