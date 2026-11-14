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
    """app_role 不得写运维元数据；吊销表只读。"""
    async with admin_engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    """
                    SELECT
                      has_table_privilege('app_role', 'outbox.maintenance_runs', 'INSERT') AS mr_insert,
                      has_table_privilege('app_role', 'outbox.maintenance_runs', 'SELECT') AS mr_select,
                      has_table_privilege('app_role', 'outbox.maintenance_runs', 'UPDATE') AS mr_update,
                      has_table_privilege('app_role', 'auth.revoked_tokens', 'SELECT') AS rt_select,
                      has_table_privilege('app_role', 'auth.revoked_tokens', 'INSERT') AS rt_insert
                    """
                )
            )
        ).one()
    assert rows.mr_insert is False
    assert rows.mr_select is False
    assert rows.mr_update is False
    assert rows.rt_select is True
    assert rows.rt_insert is False
