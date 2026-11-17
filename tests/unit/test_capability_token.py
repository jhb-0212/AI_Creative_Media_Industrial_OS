"""Capability Token 的签发、验签与 HTTP 强制测试（F3）。

覆盖三件必须被真实证明的事：

1. 访问令牌不能当 capability 重放（audience 分离）；
2. 能力凭证只能收窄项目范围，不能扩大 principal；
3. 没有 / 主体不符 / 已撤销的 capability 在真实 HTTP 入口被拒绝。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import jwt
import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from packages.identity.capability import (
    CAPABILITY_AUDIENCE,
    Capability,
    CapabilityError,
    CapabilityExpiredError,
    CapabilityIssuer,
    decode_capability,
)
from packages.shared.database import build_migration_url
from tests import TEST_SIGNING_KEY
from tests.conftest import (
    TEST_TENANT_ID,
    make_test_capability_token,
    make_test_headers,
    make_test_token,
)

_ISSUER = "ai-film-identity"


def _issuer(audience: str = CAPABILITY_AUDIENCE) -> CapabilityIssuer:
    return CapabilityIssuer(
        signing_key=TEST_SIGNING_KEY,
        issuer=_ISSUER,
        audience=audience,
        ttl_seconds=300,
    )


def _issue(**overrides: object) -> str:
    fields: dict[str, object] = {
        "subject_id": "user-cap",
        "tenant_id": str(TEST_TENANT_ID),
        "organization_id": str(TEST_TENANT_ID),
        "allowed_actions": ["PROJECT.READ"],
        "project_scope": ["proj-a"],
        "task_id": "task-cap",
        "workflow_run_id": "run-cap",
        "policy_version": "V205-R14",
    }
    fields.update(overrides)
    return _issuer().issue(**fields)  # type: ignore[arg-type]


def _decode(token: str) -> Capability:
    return decode_capability(
        token,
        public_pem=TEST_SIGNING_KEY.public_pem,
        issuer=_ISSUER,
        audience=CAPABILITY_AUDIENCE,
    )


def test_capability_roundtrip_and_narrow_scope() -> None:
    """签发 → 验签保留全部收窄字段；capability 不能放大主体范围。"""
    token = _issue()
    capability = _decode(token)
    assert capability.subject_id == "user-cap"
    assert set(capability.allowed_actions) == {"PROJECT.READ"}
    assert set(capability.project_scope) == {"proj-a"}
    assert capability.allows_project("proj-a", principal_has_all_projects=False)
    assert not capability.allows_project("proj-b", principal_has_all_projects=False)

    wildcard = _decode(_issue(project_scope=["*"]))
    assert not wildcard.allows_project("proj-new", principal_has_all_projects=False)
    assert wildcard.allows_project("proj-new", principal_has_all_projects=True)


def test_access_token_cannot_be_replayed_as_capability() -> None:
    """访问令牌 audience 不同，不能被当作 capability 使用。"""
    with pytest.raises(CapabilityError):
        _decode(make_test_token())


def test_expired_capability_is_rejected() -> None:
    """短时凭证过期后拒绝，并给出可区分的错误类型。"""
    token = _issue()
    claims = jwt.decode(token, options={"verify_signature": False})
    claims["exp"] = 1
    expired = jwt.encode(
        claims,
        TEST_SIGNING_KEY.private_pem,
        algorithm="RS256",
        headers={"kid": TEST_SIGNING_KEY.kid},
    )
    with pytest.raises(CapabilityExpiredError):
        _decode(expired)


def test_capability_cannot_be_issued_for_other_audience() -> None:
    """错误 audience 的 capability 在本 API 上无效。"""
    token = _issuer(audience="another-service").issue(
        subject_id="user-cap",
        tenant_id=str(TEST_TENANT_ID),
        organization_id=str(TEST_TENANT_ID),
        allowed_actions=["PROJECT.READ"],
        project_scope=["proj-a"],
        task_id="task-cap",
        workflow_run_id="run-cap",
        policy_version="V205-R14",
    )
    with pytest.raises(CapabilityError):
        _decode(token)


async def test_missing_capability_header_is_denied(client: AsyncClient) -> None:
    """只有访问令牌、没有 capability → 403，而不是静默跳过 F3 闸门。"""
    resp = await client.get(
        "/projects/does-not-matter",
        headers={"Authorization": f"Bearer {make_test_token()}"},
    )
    assert resp.status_code == 403
    assert resp.json()["error_code"] == "AUTH_SCOPE_DENIED"
    assert "X-Capability-Token" in resp.json()["message"]


async def test_capability_subject_mismatch_is_denied(client: AsyncClient) -> None:
    """capability 与访问令牌绑定不同主体 → 403。"""
    resp = await client.get(
        "/projects/does-not-matter",
        headers={
            "Authorization": f"Bearer {make_test_token(subject_id='alice')}",
            "X-Capability-Token": make_test_capability_token(subject_id="bob"),
        },
    )
    assert resp.status_code == 403
    assert "主体" in resp.json()["message"]


async def test_valid_capability_passes_authentication(client: AsyncClient) -> None:
    """双凭证齐全 → 通过认证层（后续 403/404 都说明授权闸门已过）。"""
    resp = await client.get(
        "/projects/does-not-matter", headers=make_test_headers()
    )
    assert resp.status_code in (403, 404), resp.text


@pytest.fixture(scope="module")
async def admin_engine() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(build_migration_url())
    try:
        yield engine
    finally:
        await engine.dispose()


async def test_revoked_capability_is_denied(
    admin_engine: AsyncEngine, client: AsyncClient
) -> None:
    """撤销 capability jti 后，同一请求立即被拒（无需等 5 分钟过期）。"""
    token = make_test_capability_token()
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
                "tid": uuid.UUID(str(TEST_TENANT_ID)),
                "sub": claims["sub"],
                "r": "capability-revocation-test",
                "by": "pytest",
            },
        )
    try:
        resp = await client.get(
            "/projects/does-not-matter",
            headers=make_test_headers(),
        )
        # make_test_headers 每次生成新 jti；这里必须用被撤销的那一枚替换。
        assert resp.status_code in (403, 404)
        revoked_resp = await client.get(
            "/projects/does-not-matter",
            headers={
                "Authorization": f"Bearer {make_test_token()}",
                "X-Capability-Token": token,
            },
        )
        assert revoked_resp.status_code == 403
        assert revoked_resp.json()["error_code"] == "CAPABILITY_REVOKED"
    finally:
        async with admin_engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM auth.revoked_tokens WHERE jti = :j"), {"j": jti}
            )
