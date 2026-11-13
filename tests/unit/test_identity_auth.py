"""身份治理（P0-3）端到端鉴权集成测试（决策 4/5 + R6/R9）。

核心验证（安全必须真实执行，测试走真实端点 + 真实 JWT）：
1. 无 Authorization 头 → 401 AUTH_UNAUTHENTICATED。
2. 伪造/篡改 token → 401（验签失败）。
3. 有效 token → 身份从 JWT 解析（created_by = subject_id，非客户端字符串）。
4. 职责分离：同一主体自锁 → 409（运行时真实主体比对）。
5. 跨租户：不同 tenant 的 token 无法读到对方数据（RLS 隔离）。
"""

from __future__ import annotations

import uuid

from httpx import AsyncClient

from tests.conftest import TEST_TENANT_ID, make_test_token


async def test_unauthenticated_request_rejected(client: AsyncClient) -> None:
    """无 Authorization 头 → 401（身份治理红线）。"""
    resp = await client.post(
        "/projects",
        json={"project_name": "未认证项目", "project_tier": "B"},
    )
    assert resp.status_code == 401
    assert resp.json()["error_code"] == "AUTH_UNAUTHENTICATED"


async def test_forged_token_rejected(client: AsyncClient) -> None:
    """伪造/篡改 token → 401（验签失败，R6 安全真实执行）。"""
    # 一段明显非法的 token（不是合法 JWT，也没有有效签名）。
    forged = "eyJhbGciOiJSUzI1NiIsImtpZCI6ImN1cnJlbnQifQ.eyJzdWIiOiJoYWNrZXIifQ.forged"
    resp = await client.post(
        "/projects",
        json={"project_name": "伪造项目", "project_tier": "B"},
        headers={"Authorization": f"Bearer {forged}"},
    )
    assert resp.status_code == 401


async def test_wrong_tenant_token_isolated(auth_client: AsyncClient) -> None:
    """不同租户的 token 无法读到对方的项目（RLS 跨租户隔离）。

    先用默认租户 token 创建项目，再用「另一个租户」的 token 查询，
    应 404（RLS 隔离，看不到对方数据）。
    """
    eid = f"proj_iso_{uuid.uuid4().hex[:8]}"
    created = await auth_client.post(
        "/projects",
        json={"project_name": "隔离测试项目", "project_tier": "B", "entity_id": eid},
    )
    assert created.status_code == 201

    # 另一个租户的 token（tenant_id 不同）。
    other_tenant = uuid.UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    other_token = make_test_token(subject_id="other_user", tenant_id=other_tenant)
    # 用另一个租户查询，应 404（RLS 隔离，看不到默认租户的项目）。
    resp = await auth_client.get(
        f"/projects/{eid}",
        headers={"Authorization": f"Bearer {other_token}"},
    )
    assert resp.status_code == 404


async def test_identity_from_jwt_not_request_body(auth_client: AsyncClient) -> None:
    """身份来自 JWT（subject_id），而非客户端可伪造的请求体字符串。

    请求体里即使试图传 created_by，也会被忽略（请求体 schema 已无该字段），
    实际 created_by 恒等于 token 的 sub。
    """
    resp = await auth_client.post(
        "/projects",
        json={
            "project_name": "身份来源验证",
            "project_tier": "B",
            # 客户端尝试伪造身份（但请求体已无 created_by 字段，被忽略）。
            "created_by": "attacker_forged_identity",
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    # 真实身份来自 JWT 的 sub（user_script），非请求体的伪造值。
    assert body["created_by"] == "user_script"
    assert body["tenant_id"] == str(TEST_TENANT_ID)
