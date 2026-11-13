"""RS256 JWT 身份内核单元测试（决策 4 + R6/R9）。

验证真实行为（非配置结构）：
1. 私钥签发 → 公钥验签 → 解析出可信 Principal（round-trip）。
2. 篡改令牌 / 换密钥验签 → 拒绝（签名真实性）。
3. issuer / audience 不匹配 → 拒绝（跨服务令牌隔离）。
4. 过期令牌 → 拒绝（短期强制过期）。
5. tenant_id 非 UUID → 拒绝（RLS 注入防线）。
6. JWKS 只导出公钥、绝不泄漏私钥（密钥红线）。
"""

from __future__ import annotations

import uuid

import pytest

from packages.identity.jwks import KeyStore
from packages.identity.jwt_core import (
    TokenError,
    decode_token,
    encode_token,
)
from packages.identity.keys import SigningKey, generate_signing_key
from packages.identity.principal import Principal

ISSUER = "ai-film-identity"
AUDIENCE = "ai-film-api"
TENANT_ID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def key() -> SigningKey:
    return generate_signing_key()


def _encode(subject_id: str = "user-001", roles: list[str] | None = None, **kwargs: object) -> str:
    key = kwargs.pop("_key", None) or generate_signing_key()
    return encode_token(
        key,
        subject_id=subject_id,
        tenant_id=TENANT_ID,
        roles=roles or ["导演"],
        issuer=ISSUER,
        audience=AUDIENCE,
        **kwargs,  # type: ignore[arg-type]
    )


def test_round_trip_decode_to_principal(key: SigningKey) -> None:
    """签发后验签，还原出可信 Principal（含 tenant_id / roles / jti）。"""
    k = key
    token = encode_token(
        k,
        subject_id="user-001",
        tenant_id=TENANT_ID,
        roles=["导演", "编剧"],
        issuer=ISSUER,
        audience=AUDIENCE,
    )
    principal = decode_token(
        token, public_pem=k.public_pem, issuer=ISSUER, audience=AUDIENCE
    )
    assert isinstance(principal, Principal)
    assert principal.subject_id == "user-001"
    assert str(principal.tenant_id) == TENANT_ID
    assert principal.has_role("导演")
    assert principal.has_role("编剧")
    assert principal.jti is not None


def test_tampered_token_rejected(key: SigningKey) -> None:
    """篡改 payload 后验签失败（签名真实性）。"""
    k = key
    token = encode_token(
        k, subject_id="user-001", tenant_id=TENANT_ID, roles=["导演"],
        issuer=ISSUER, audience=AUDIENCE,
    )
    # 篡改 payload（第二段，base64url 编码）：把 subject_id 换成另一个主体。
    import base64

    header, payload, signature = token.split(".")
    decoded = base64.urlsafe_b64decode(payload + "==").decode("utf-8")
    forged = decoded.replace("user-001", "admin-000")
    forged_payload = base64.urlsafe_b64encode(forged.encode("utf-8")).rstrip(b"=").decode()
    tampered = f"{header}.{forged_payload}.{signature}"
    with pytest.raises(TokenError):
        decode_token(tampered, public_pem=k.public_pem, issuer=ISSUER, audience=AUDIENCE)


def test_wrong_key_rejected() -> None:
    """用另一把密钥的公钥验签失败（密钥隔离）。"""
    signing = generate_signing_key()
    other = generate_signing_key()
    token = encode_token(
        signing, subject_id="user-001", tenant_id=TENANT_ID, roles=["导演"],
        issuer=ISSUER, audience=AUDIENCE,
    )
    with pytest.raises(TokenError):
        decode_token(token, public_pem=other.public_pem, issuer=ISSUER, audience=AUDIENCE)


def test_wrong_issuer_rejected(key: SigningKey) -> None:
    """issuer 不匹配拒绝（跨服务令牌隔离）。"""
    k = key
    token = encode_token(
        k, subject_id="user-001", tenant_id=TENANT_ID, roles=["导演"],
        issuer=ISSUER, audience=AUDIENCE,
    )
    with pytest.raises(TokenError):
        decode_token(token, public_pem=k.public_pem, issuer="evil-issuer", audience=AUDIENCE)


def test_wrong_audience_rejected(key: SigningKey) -> None:
    """audience 不匹配拒绝。"""
    k = key
    token = encode_token(
        k, subject_id="user-001", tenant_id=TENANT_ID, roles=["导演"],
        issuer=ISSUER, audience=AUDIENCE,
    )
    with pytest.raises(TokenError):
        decode_token(token, public_pem=k.public_pem, issuer=ISSUER, audience="evil-aud")


def test_expired_token_rejected(key: SigningKey) -> None:
    """过期令牌拒绝（短期强制过期）。"""
    k = key
    # 过期 60 秒（超出 30 秒 leeway 窗口），确保被拒绝。
    token = encode_token(
        k, subject_id="user-001", tenant_id=TENANT_ID, roles=["导演"],
        issuer=ISSUER, audience=AUDIENCE, ttl_seconds=-60,
    )
    with pytest.raises(TokenError):
        decode_token(token, public_pem=k.public_pem, issuer=ISSUER, audience=AUDIENCE)


def test_invalid_tenant_rejected(key: SigningKey) -> None:
    """tenant_id 非 UUID 拒绝（RLS 注入防线）。"""
    k = key
    token = encode_token(
        k, subject_id="user-001", tenant_id="not-a-uuid' OR '1'='1", roles=["导演"],
        issuer=ISSUER, audience=AUDIENCE,
    )
    with pytest.raises(TokenError):
        decode_token(token, public_pem=k.public_pem, issuer=ISSUER, audience=AUDIENCE)


def test_jwks_exports_only_public_key() -> None:
    """JWKS 只含公钥，绝不含私钥内容（密钥红线）。"""
    key = generate_signing_key()
    store = KeyStore([key])
    jwks = store.jwks()
    assert "keys" in jwks
    keys = jwks["keys"]
    assert isinstance(keys, list)
    assert len(keys) == 1
    jwk = keys[0]
    assert jwk["kty"] == "RSA"
    assert jwk["kid"] == key.kid
    # 断言不含私钥字段（私钥 PEM 有 "PRIVATE KEY"，公钥 JWK 只含 n/e）。
    assert "d" not in jwk
    assert "p" not in jwk
    assert "q" not in jwk
    # 私钥 PEM 绝不进入 JWKS。
    assert "PRIVATE" not in str(jwks).upper() or "PRIVATE" not in str(jwk).upper()


def test_keystore_lookup_and_current(key: SigningKey) -> None:
    """KeyStore 按 kid 命中、current 取最新密钥。"""
    k1 = generate_signing_key()
    k2 = generate_signing_key()
    store = KeyStore([k1, k2])
    assert store.current.kid == k2.kid
    assert store.get(k1.kid) is k1
    assert store.get(k2.kid) is k2
    assert store.get("nonexistent") is None
    assert store.public_pem(k1.kid) == k1.public_pem


def test_principal_immutable_and_roles() -> None:
    """Principal 不可变、角色判定正确。"""
    p = Principal(
        subject_id="user-001",
        tenant_id=uuid.UUID(TENANT_ID),
        roles=frozenset(["导演", "编剧"]),
    )
    assert p.has_role("导演")
    assert p.has_any_role("制片", "导演")
    assert not p.has_role("制片")
    with pytest.raises(AttributeError):
        p.subject_id = "hacked"  # type: ignore[misc]
