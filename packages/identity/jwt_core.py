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
from packages.identity.principal import Principal, SubjectType

# JWT 算法固定 RS256（决策 4，不接受其它算法降级）。
ALGORITHM = "RS256"

# 标准 claim 键名（单一事实源，避免散落魔法字符串）。
CLAIM_SUB = "sub"
CLAIM_SUB_TYPE = "sub_type"
CLAIM_TENANT = "tenant_id"
CLAIM_ORGANIZATION = "org_id"
CLAIM_PROJECT_IDS = "project_ids"
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
    subject_type: str = SubjectType.HUMAN.value,
    organization_id: str | None = None,
    project_ids: list[str] | None = None,
) -> str:
    """用私钥签发一条 RS256 访问令牌。

    参数：
    - signing_key：含私钥的密钥对（私钥仅在此处用于签发）。
    - subject_id / tenant_id / roles：主体身份与租户上下文。
    - issuer / audience：令牌签发方与目标受众（验签时严格比对）。
    - ttl_seconds：有效期（默认 15 分钟）。
    - subject_type：主体类型（human/agent/service）。授权决策据此判定
      CRITICAL 动作能否通过——类型必须由签发端写死，不能由调用方自报。
    - organization_id：组织归属；缺省与 tenant_id 相同（单一组织租户的基线
      形态）。多组织租户必须显式传，否则默认会落到"组织=租户"这一档。
    - project_ids：可访问项目范围；缺省为空集（一个项目也访问不了），
      要做组织级访问必须显式传 ``["*"]``（``*`` 的单位是组织，不是租户）。

    返回：编码后的 JWT 字符串（三段式，含 header.payload.signature）。
    """
    now = datetime.now(UTC)
    claims: dict[str, Any] = {
        CLAIM_SUB: subject_id,
        CLAIM_SUB_TYPE: subject_type,
        CLAIM_TENANT: tenant_id,
        CLAIM_ORGANIZATION: organization_id or tenant_id,
        CLAIM_PROJECT_IDS: list(project_ids or []),
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
    organization_raw = payload.get(CLAIM_ORGANIZATION)
    if not isinstance(subject_id, str) or not subject_id.strip():
        raise TokenError("令牌缺少合法的 sub claim")
    if not isinstance(tenant_id_raw, str) or not tenant_id_raw.strip():
        raise TokenError("令牌缺少 tenant_id claim")
    if not isinstance(organization_raw, str) or not organization_raw.strip():
        # 不做"缺 org_id 就用 tenant_id"的兜底：兜底会让漏签 claim 的令牌
        # 悄悄获得"组织=租户"的宽范围，而报错能在签发端立刻暴露问题。
        raise TokenError("令牌缺少 org_id claim")

    try:
        tenant_id = uuid.UUID(tenant_id_raw)
    except ValueError as exc:
        raise TokenError(f"tenant_id 不是合法 UUID: {tenant_id_raw!r}") from exc
    try:
        organization_id = uuid.UUID(organization_raw)
    except ValueError as exc:
        raise TokenError(f"org_id 不是合法 UUID: {organization_raw!r}") from exc

    roles_raw = payload.get(CLAIM_ROLES, [])
    # fail-closed：roles 必须是字符串数组。字符串会被迭代成单字（"导演" →
    # {"导","演"}），导致 RBAC 角色集错误；非法类型一律拒绝而非静默降级。
    if not isinstance(roles_raw, list) or any(
        not isinstance(r, str) for r in roles_raw
    ):
        raise TokenError("roles claim 必须是字符串数组")
    roles = frozenset(roles_raw)

    project_ids_raw = payload.get(CLAIM_PROJECT_IDS, [])
    if not isinstance(project_ids_raw, list) or any(
        not isinstance(p, str) for p in project_ids_raw
    ):
        raise TokenError("project_ids claim 必须是字符串数组")

    subject_type_raw = payload.get(CLAIM_SUB_TYPE, SubjectType.HUMAN.value)
    try:
        subject_type = SubjectType(subject_type_raw)
    except ValueError as exc:
        raise TokenError(f"未知的 sub_type: {subject_type_raw!r}") from exc

    return Principal(
        subject_id=subject_id,
        tenant_id=tenant_id,
        organization_id=organization_id,
        subject_type=subject_type,
        roles=roles,
        project_ids=frozenset(project_ids_raw),
        jti=payload.get(CLAIM_JTI) if isinstance(payload.get(CLAIM_JTI), str) else None,
    )
