"""JWT 吊销（jti denylist）与权限基线（P3-2 / P3-5）。"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import jwt
import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from packages.shared.database import build_migration_url
from packages.shared.db_roles import DatabaseDomain, all_business_roles, role_for
from tests.conftest import make_test_token


@pytest.fixture(scope="module")
async def admin_engine() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(build_migration_url())
    try:
        yield engine
    finally:
        await engine.dispose()


async def test_revoked_jti_is_rejected(
    admin_engine: AsyncEngine, client: AsyncClient
) -> None:
    token = make_test_token()
    claims = jwt.decode(token, options={"verify_signature": False})
    jti = claims["jti"]

    async with admin_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO auth.revoked_tokens "
                "(jti, tenant_id, subject_id, expires_at, reason, revoked_by) "
                "VALUES (:jti, :tid, :sub, now() + interval '1 hour', :r, :by)"
            ),
            {
                "jti": jti,
                "tid": uuid.UUID(claims["tenant_id"]),
                "sub": claims["sub"],
                "r": "integration-test",
                "by": "pytest",
            },
        )

    try:
        resp = await client.get(
            "/projects/does-not-matter",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 401, resp.text
        assert resp.json()["error_code"] == "AUTH_UNAUTHENTICATED"
    finally:
        async with admin_engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM auth.revoked_tokens WHERE jti = :j"),
                {"j": jti},
            )

    # 未吊销时同一令牌可以正常通过认证（403/404 都说明已过认证）。
    resp = await client.get(
        "/projects/does-not-matter",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code in (403, 404), resp.text


async def test_grant_baseline(admin_engine: AsyncEngine) -> None:
    """按域最小权限基线：维护元数据无人可写；吊销表只给身份域。

    断言对象从"单个 app_role"换成"全部业务域角色"，因为角色拆分后
    ``app_role`` 已不存在——继续断言一个不存在的角色会让测试永远绿，
    反而掩盖"新角色拿多了权限"。
    """
    async with admin_engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    """
                    SELECT
                      r.rolname,
                      has_table_privilege(
                        r.rolname, 'outbox.maintenance_runs', 'INSERT') AS mr_insert,
                      has_table_privilege(
                        r.rolname, 'outbox.maintenance_runs', 'SELECT') AS mr_select,
                      has_table_privilege(
                        r.rolname, 'auth.revoked_tokens', 'SELECT') AS rt_select,
                      has_table_privilege(
                        r.rolname, 'auth.revoked_tokens', 'INSERT') AS rt_insert
                    FROM pg_roles r
                    WHERE r.rolname = ANY(:roles)
                    """
                ),
                {"roles": list(all_business_roles())},
            )
        ).mappings().all()

    assert {row["rolname"] for row in rows} == set(all_business_roles()), (
        "业务域角色不齐：数据库里缺角色或 db_roles 登记多出角色"
    )

    identity_role = role_for(DatabaseDomain.IDENTITY)
    for row in rows:
        role = row["rolname"]
        # 维护记录只由维护 CLI（迁移身份）读写，任何应用角色都不该碰。
        assert row["mr_insert"] is False, f"{role} 能写维护记录"
        assert row["mr_select"] is False, f"{role} 能读维护记录"
        if role == identity_role:
            assert row["rt_select"] is True, "身份域读不到吊销表"
            assert row["rt_insert"] is True, "身份域无法写入吊销记录"
        else:
            # 其他域一律不得接触吊销表：吊销检查走身份域连接池。
            assert row["rt_select"] is False, f"{role} 能读吊销表"
            assert row["rt_insert"] is False, f"{role} 能写吊销表"

    async with admin_engine.connect() as conn:
        legacy = await conn.scalar(
            text("SELECT count(*) FROM pg_roles WHERE rolname = 'app_role'")
        )
    assert legacy == 0, "旧的跨 schema 角色 app_role 仍存在"
