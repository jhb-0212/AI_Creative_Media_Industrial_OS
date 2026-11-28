"""Project API 集成测试（异步，连真实 PostgreSQL，走真实鉴权）。

说明：
- 使用 httpx.AsyncClient + ASGITransport，避免 TestClient 与 asyncpg 的事件循环冲突。
- 依赖 docker-compose 的 PostgreSQL（独立 schema project_planning）。
- 身份治理（P0-3）：请求带 Authorization Bearer，created_by / tenant_id 从 JWT
  解析，非客户端字符串；测试数据用唯一前缀避免相互污染。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from httpx import AsyncClient
from sqlalchemy import text

from tests.conftest import TEST_TENANT_ID


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


async def _seed_organization(admin_sessionmaker, org_id: uuid.UUID) -> None:
    """播种组织行：复合外键 (organization_id, tenant_id) 要求组织真实存在。

    组织与项目必须同租户（TEST_TENANT_ID，与测试令牌的 tenant_id claim 一致），
    否则复合外键会在数据库层拒绝——那正是 SCOPE-1 想要的负向行为，不该被
    本组用例（组织范围 ABAC）误触发。

    **为什么用管理连接而不是业务连接**（DATA-6 之后的新约束）：契约里没有任何
    ``ORGANIZATION.*`` 动作，组织是平台级归属根，业务域角色只有 SELECT。
    播种是**测试基础设施**，对应的是「管理面创建组织」这条尚不存在的路径，
    用业务角色播不出来才是对的。若改用放宽授权来让播种成功，就等于用测试
    需求反过来削弱生产的最小权限——方向反了。

    管理连接本身不受 RLS 约束（超级用户），因此不需要注入作用域；这也是
    fixture 先断言 superuser 的原因（前提不成立时用例必须报错而不是静默通过）。
    """
    async with admin_sessionmaker() as session:
        await session.execute(
            text(
                """
                INSERT INTO project_planning.organizations
                    (organization_id, tenant_id, name, created_at, created_by, updated_at)
                VALUES (:org, :tid, '组织范围测试组织', :now, 'tester', :now)
                ON CONFLICT DO NOTHING
                """
            ),
            {"org": str(org_id), "tid": str(TEST_TENANT_ID), "now": datetime.now(UTC)},
        )
        await session.commit()


def _client_with_org(
    organization_id: uuid.UUID | None, subject_id: str = "org_user"
) -> AsyncClient:
    """构造带指定 org claim 的 client（真实 JWT + 真实 ASGI 端点）。"""
    from httpx import ASGITransport

    from apps.api.main import app
    from tests.conftest import make_test_token

    transport = ASGITransport(app=app)
    token = make_test_token(subject_id=subject_id, organization_id=organization_id)
    headers = {"Authorization": f"Bearer {token}"}
    return AsyncClient(transport=transport, base_url="http://test", headers=headers)


async def test_create_project_in_other_organization_rejected(admin_sessionmaker) -> None:
    """A 组织成员不得把项目挂到 B 组织 → 404（B 组织对 A 不可见）。

    语义（ADR-R24）：组织边界下沉到数据库后，「他组织」不是「看得见但无权碰」，
    而是**看不见**——organization 解析在 RLS 之下拿不到 B 的行，于是归属路径
    解析不出，返回与「不存在」同形的 404。这比 403 更严格：403 仍承认了
    「B 组织存在」这一事实，404 连存在性都不泄漏。

    前置条件必须显式播种 B 组织：本用例的语义是「同租户、**存在但不同**的
    组织」。不播种也能得到 404，但那是「真的不存在」——测到的就不是本条要
    证明的语义了。
    """
    await _seed_organization(admin_sessionmaker, _ORG_B)
    async with _client_with_org(_ORG_A) as c:
        resp = await c.post(
            "/projects",
            json={
                "project_name": f"越权组织项目_{uuid.uuid4().hex[:8]}",
                "project_tier": "B",
                "organization_id": str(_ORG_B),
            },
        )
    assert resp.status_code == 404
    assert resp.json()["error_code"] == "NOT_FOUND"


async def test_create_project_in_own_organization_succeeds(admin_sessionmaker) -> None:
    """A 组织成员在自己的组织下创建项目 → 201（不误伤）。"""
    await _seed_organization(admin_sessionmaker, _ORG_A)
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


async def test_personal_principal_cannot_create_organization_project(
    admin_sessionmaker,
) -> None:
    """无 org claim 的个人主体不能创建组织项目（fail-closed）→ 404。

    个人主体的会话组织上下文为空，``organization_id IS NULL OR organization_id
    = current_org`` 只剩「个人行可见」，于是任何组织行都解析不到 → 404。
    组织行显式播种，确保 404 来自「不可见」而不是「不存在」（ADR-R24）。
    """
    await _seed_organization(admin_sessionmaker, _ORG_A)
    async with _client_with_org(None) as c:
        resp = await c.post(
            "/projects",
            json={
                "project_name": f"个人主体越权_{uuid.uuid4().hex[:8]}",
                "project_tier": "B",
                "organization_id": str(_ORG_A),
            },
        )
    assert resp.status_code == 404
    assert resp.json()["error_code"] == "NOT_FOUND"


async def test_org_project_read_by_other_organization_rejected(admin_sessionmaker) -> None:
    """A 组织创建的项目，B 组织成员读取 → 404（租户一致、组织不同）。

    同租户、组织不同：项目行真实存在于同一个租户里，但被组织谓词过滤掉，
    于是「他组织的项目」与「不存在的项目」在响应上完全同形（ADR-R24）。
    """
    await _seed_organization(admin_sessionmaker, _ORG_A)
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
    assert resp.status_code == 404
    assert resp.json()["error_code"] == "NOT_FOUND"


async def test_org_project_read_by_same_organization_succeeds(admin_sessionmaker) -> None:
    """同组织成员读取组织项目 → 200（不误伤）。"""
    await _seed_organization(admin_sessionmaker, _ORG_A)
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


async def test_org_project_status_change_by_other_organization_rejected(admin_sessionmaker) -> None:
    """B 组织成员推进 A 组织项目的状态 → 404，且项目状态不被改变。

    「不可见即不可推进」比「可见但拒绝」更强：读不到的行连状态转移的入口
    都不存在，`advance_status` 的守卫（乐观锁 + 状态机）根本不会被触发。
    """
    await _seed_organization(admin_sessionmaker, _ORG_A)
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
    assert resp.status_code == 404
    assert resp.json()["error_code"] == "NOT_FOUND"

    # 落库前校验：状态不得已被推进（fail-closed，不是事后补救）。
    async with _client_with_org(_ORG_A, subject_id="org_a_creator") as c:
        reread = await c.get(f"/projects/{eid}")
    assert reread.status_code == 200
    assert reread.json()["status"] == "DRAFT"
