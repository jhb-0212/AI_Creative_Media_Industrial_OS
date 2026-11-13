"""RS256 JWT 签发与验签（身份可信来源的编解码层）。

设计要点（决策 4）：
- **RS256 非对称签名**：私钥签发、公钥验签，杜绝 HS256 共享密钥泄露导致的
  全盘崩溃；密钥轮换对验签方无感（按 kid 取公钥）。
- **短期过期**：访问令牌默认 15 分钟过期（access_token_ttl_seconds），
  配合唯一 jti 防重放。
- **issuer / audience 严格校验**：拒绝跨域/跨服务的令牌。

分层：本模块只负责 JWT 编解码，产出/消费 Principal（principal.py），
不涉及任何 HTTP / FastAPI 依赖，保证可独立单元测试。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from jwt.exceptions import InvalidTokenError

from packages.identity.keys import SigningKey
from packages.identity.principal import Principal

# JWT 算法固定 RS256（决策 4，不接受其它算法降级）。
ALGORITHM = "RS256"

# 标准 claim 键名（单一事实源，避免散落魔法字符串）。
CLAIM_SUB = "sub"
CLAIM_TENANT = "tenant_id"
CLAIM_ROLES = "roles"
CLAIM_JTI = "jti"
CLAIM_ISSUER = "iss"
CLAIM_AUDIENCE = "aud"
CLAIM_EXP = "exp"
CLAIM_IAT = "iat"


class TokenError(Exception):
    """令牌编解码错误基类（验签失败/过期/claim 非法统一走此类型）。"""


def encode_token(
    signing_key: SigningKey,
    *,
    subject_id: str,
    tenant_id: str,
    roles: list[str],
    issuer: str,
    audience: str,
    ttl_seconds: int = 900,
) -> str:
    """用私钥签发一条 RS256 访问令牌。

    参数：
    - signing_key：含私钥的密钥对（私钥仅在此处用于签发）。
    - subject_id / tenant_id / roles：主体身份与租户上下文。
    - issuer / audience：令牌签发方与目标受众（验签时严格比对）。
    - ttl_seconds：有效期（默认 15 分钟）。

    返回：编码后的 JWT 字符串（三段式，含 header.payload.signature）。
    """
    now = datetime.now(UTC)
    claims: dict[str, Any] = {
        CLAIM_SUB: subject_id,
        CLAIM_TENANT: tenant_id,
        CLAIM_ROLES: list(roles),
        CLAIM_JTI: uuid.uuid4().hex,
        CLAIM_ISSUER: issuer,
        CLAIM_AUDIENCE: audience,
        CLAIM_IAT: now,
        CLAIM_EXP: now + timedelta(seconds=ttl_seconds),
    }
    # header 显式携带 kid：验签方据此从 JWKS/KeyStore 按 kid 取公钥（决策 4）。
    headers = {"kid": signing_key.kid}
    return jwt.encode(
        claims, signing_key.private_pem, algorithm=ALGORITHM, headers=headers
    )


def decode_token(
    token: str,
    *,
    public_pem: bytes,
    issuer: str,
    audience: str,
    leeway_seconds: int = 30,
) -> Principal:
    """用公钥验签并解析出可信 Principal。

    严格校验：
    - 签名有效（RS256 公钥验签，失败抛 TokenError）。
    - issuer / audience 精确匹配（拒绝跨服务令牌）。
    - 过期时间（exp）校验，容忍 leeway 秒的时钟偏差。
    - sub / tenant_id 存在且 tenant_id 是合法 UUID。

    返回：从可信 claims 构造的 Principal（身份的唯一可信出口）。
    任何失败抛 TokenError，由调用方转换为 401 AUTH_UNAUTHENTICATED。
    """
    try:
        payload = jwt.decode(
            token,
            public_pem,
            algorithms=[ALGORITHM],
            issuer=issuer,
            audience=audience,
            leeway=leeway_seconds,
        )
    except InvalidTokenError as exc:
        raise TokenError(f"令牌验签失败: {exc}") from exc

    subject_id = payload.get(CLAIM_SUB)
    tenant_id_raw = payload.get(CLAIM_TENANT)
    if not isinstance(subject_id, str) or not subject_id.strip():
        raise TokenError("令牌缺少合法的 sub claim")
    if not isinstance(tenant_id_raw, str) or not tenant_id_raw.strip():
        raise TokenError("令牌缺少 tenant_id claim")

    try:
        tenant_id = uuid.UUID(tenant_id_raw)
    except ValueError as exc:
        raise TokenError(f"tenant_id 不是合法 UUID: {tenant_id_raw!r}") from exc

    roles_raw = payload.get(CLAIM_ROLES, [])
    roles = frozenset(r for r in roles_raw if isinstance(r, str))

    return Principal(
        subject_id=subject_id,
        tenant_id=tenant_id,
        roles=roles,
        jti=payload.get(CLAIM_JTI) if isinstance(payload.get(CLAIM_JTI), str) else None,
    )
