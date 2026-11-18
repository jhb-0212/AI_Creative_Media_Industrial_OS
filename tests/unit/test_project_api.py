"""Project API 集成测试（异步，连真实 PostgreSQL，走真实鉴权）。

说明：
- 使用 httpx.AsyncClient + ASGITransport，避免 TestClient 与 asyncpg 的事件循环冲突。
- 依赖 docker-compose 的 PostgreSQL（独立 schema project_planning）。
- 身份治理（P0-3）：请求带 Authorization Bearer，created_by / tenant_id 从 JWT
  解析，非客户端字符串；测试数据用唯一前缀避免相互污染。
"""

from __future__ import annotations

import uuid

from httpx import AsyncClient


async def test_create_project_success(auth_client: AsyncClient) -> None:
    resp = await auth_client.post(
        "/projects",
        json={
            "project_name": f"测试项目_{uuid.uuid4().hex[:8]}",
            "project_tier": "A",
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "DRAFT"
    assert body["project_tier"] == "A"
    assert body["entity_id"]
    assert body["created_by"] == "user_script"  # 身份来自 JWT，非请求体
    # 租户是内部隔离键，不属于对外契约（object_schemas 无 tenant_id）。
    assert "tenant_id" not in body


async def test_create_project_idempotent(auth_client: AsyncClient) -> None:
    """幂等：同一 entity_id 重复创建返回同一项目，不产生第二条（D3 精神）。"""
    eid = f"proj_idem_{uuid.uuid4().hex[:8]}"
    payload = {
        "project_name": "幂等项目",
        "project_tier": "B",
        "entity_id": eid,
    }
    r1 = await auth_client.post("/projects", json=payload)
    r2 = await auth_client.post("/projects", json=payload)
    assert r1.status_code == 201
    assert r2.status_code == 201
    assert r1.json()["entity_id"] == r2.json()["entity_id"] == eid
    assert r1.json()["entity_version"] == r2.json()["entity_version"] == 1


async def test_create_project_rejects_empty_name(auth_client: AsyncClient) -> None:
    resp = await auth_client.post(
        "/projects",
        json={"project_name": "  ", "project_tier": "B"},
    )
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "VALIDATION_ERROR"


async def test_create_project_rejects_unauthenticated(client: AsyncClient) -> None:
    """无认证请求被拒绝（401）——身份治理红线，安全必须真实执行。"""
    resp = await client.post(
        "/projects",
        json={"project_name": "未认证项目", "project_tier": "B"},
    )
    assert resp.status_code == 401


async def test_get_project_not_found(auth_client: AsyncClient) -> None:
    resp = await auth_client.get(f"/projects/not_exist_{uuid.uuid4().hex[:8]}")
    assert resp.status_code == 404
    assert resp.json()["error_code"] == "NOT_FOUND"


async def test_get_project_roundtrip(auth_client: AsyncClient) -> None:
    """创建后查询，验证真实落库（读写闭环）。"""
    eid = f"proj_rt_{uuid.uuid4().hex[:8]}"
    created = await auth_client.post(
        "/projects",
        json={
            "project_name": "落库验证项目",
            "project_tier": "S",
            "entity_id": eid,
        },
    )
    assert created.status_code == 201

    fetched = await auth_client.get(f"/projects/{eid}")
    assert fetched.status_code == 200
    body = fetched.json()
    assert body["entity_id"] == eid
    assert body["project_name"] == "落库验证项目"
    assert body["project_tier"] == "S"


# ---------------------------------------------------------------------------
# 组织范围授权（Codex P1 #10）：租户 RLS 只隔离租户，不隔离租户内的组织。
# 组织项目的 create/read/status-change 必须与主体的 org claim 匹配（资源级
# ABAC，基线 27.3 多层级作用域），fail-closed。
# ---------------------------------------------------------------------------

_ORG_A = uuid.uuid4()
_ORG_B = uuid.uuid4()


def _client_with_org(organization_id: uuid.UUID | None, subject_id: str = "org_user") -> AsyncClient:
    """构造带指定 org claim 的 client（真实 JWT + 真实 ASGI 端点）。"""
    from httpx import ASGITransport

    from apps.api.main import app
    from tests.conftest import make_test_token

    transport = ASGITransport(app=app)
    token = make_test_token(
        subject_id=subject_id, organization_id=organization_id
    )
    headers = {"Authorization": f"Bearer {token}"}
    return AsyncClient(transport=transport, base_url="http://test", headers=headers)


async def test_create_project_in_other_organization_rejected() -> None:
    """A 组织成员不得把项目挂到 B 组织：创建即越权 → 403。"""
    async with _client_with_org(_ORG_A) as c:
        resp = await c.post(
            "/projects",
            json={
                "project_name": f"越权组织项目_{uuid.uuid4().hex[:8]}",
                "project_tier": "B",
                "organization_id": str(_ORG_B),
            },
        )
    assert resp.status_code == 403
    assert resp.json()["error_code"] == "AUTH_SCOPE_DENIED"


async def test_create_project_in_own_organization_succeeds() -> None:
    """A 组织成员在自己的组织下创建项目 → 201（不误伤）。"""
    async with _client_with_org(_ORG_A) as c:
        resp = await c.post(
            "/projects",
            json={
                "project_name": f"本组织项目_{uuid.uuid4().hex[:8]}",
                "project_tier": "B",
                "organization_id": str(_ORG_A),
            },
        )
    assert resp.status_code == 201


async def test_personal_principal_cannot_create_organization_project() -> None:
    """无 org claim 的个人主体不能创建组织项目（fail-closed）→ 403。"""
    async with _client_with_org(None) as c:
        resp = await c.post(
            "/projects",
            json={
                "project_name": f"个人主体越权_{uuid.uuid4().hex[:8]}",
                "project_tier": "B",
                "organization_id": str(_ORG_A),
            },
        )
    assert resp.status_code == 403
    assert resp.json()["error_code"] == "AUTH_SCOPE_DENIED"


async def test_org_project_read_by_other_organization_rejected() -> None:
    """A 组织创建的项目，B 组织成员读取 → 403（租户一致、组织不同）。"""
    async with _client_with_org(_ORG_A, subject_id="org_a_creator") as c:
        created = await c.post(
            "/projects",
            json={
                "project_name": f"读取隔离_{uuid.uuid4().hex[:8]}",
                "project_tier": "B",
                "organization_id": str(_ORG_A),
            },
        )
    assert created.status_code == 201
    eid = created.json()["entity_id"]

    async with _client_with_org(_ORG_B, subject_id="org_b_reader") as c:
        resp = await c.get(f"/projects/{eid}")
    assert resp.status_code == 403
    assert resp.json()["error_code"] == "AUTH_SCOPE_DENIED"


async def test_org_project_read_by_same_organization_succeeds() -> None:
    """同组织成员读取组织项目 → 200（不误伤）。"""
    async with _client_with_org(_ORG_A, subject_id="org_a_creator") as c:
        created = await c.post(
            "/projects",
            json={
                "project_name": f"同组织读取_{uuid.uuid4().hex[:8]}",
                "project_tier": "B",
                "organization_id": str(_ORG_A),
            },
        )
        eid = created.json()["entity_id"]
        resp = await c.get(f"/projects/{eid}")
    assert resp.status_code == 200


async def test_org_project_status_change_by_other_organization_rejected() -> None:
    """B 组织成员推进 A 组织项目的状态 → 403，且项目状态不被改变。"""
    async with _client_with_org(_ORG_A, subject_id="org_a_creator") as c:
        created = await c.post(
            "/projects",
            json={
                "project_name": f"状态越权_{uuid.uuid4().hex[:8]}",
                "project_tier": "B",
                "organization_id": str(_ORG_A),
            },
        )
        eid = created.json()["entity_id"]

    async with _client_with_org(_ORG_B, subject_id="org_b_actor") as c:
        resp = await c.post(
            f"/projects/{eid}/status",
            json={"target_status": "ACTIVE"},
        )
    assert resp.status_code == 403
    assert resp.json()["error_code"] == "AUTH_SCOPE_DENIED"

    # 落库前校验：状态不得已被推进（fail-closed，不是事后补救）。
    async with _client_with_org(_ORG_A, subject_id="org_a_creator") as c:
        reread = await c.get(f"/projects/{eid}")
    assert reread.status_code == 200
    assert reread.json()["status"] == "DRAFT"
