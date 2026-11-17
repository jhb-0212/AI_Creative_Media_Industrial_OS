"""Capability Token 的最小运行时契约（RS256，短时、限范围、可撤销）。

为什么单独一层，而不是塞进访问令牌：

- 访问令牌回答"你是谁"，有效期 15 分钟，范围是整个 principal；
- Capability Token 回答"这一次你能对哪些资源做哪些动作"，有效期默认 5 分钟，
  并且**只能收窄**访问令牌的范围。

两者受众（`aud`）不同，访问令牌无法被当作 capability 重放。签发私钥只存在于
独立 Authorization Service / issuer 进程；API 进程只持有公钥，结构上不能自签。

本模块与 ``principal.py`` 的边界：本模块不 import Principal，只产出不可变的
``Capability`` 值对象；Principal 持有它，授权决策在 ``authorization.py`` 里统一消费。

契约依据：基线 §10.5 与 §C.5.1。已实现的字段全部参与类型校验；
预算金额、资源配额、委托链与审批引用被保留并进入审计，但实际预算扣减/
配额占用属于各自业务域的命令职责，不在本模块重复实现。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, cast

import jwt
from jwt.exceptions import ExpiredSignatureError, InvalidTokenError

from packages.identity.keys import SigningKey

ALGORITHM = "RS256"

#: capability 专用受众；访问令牌的 audience 是 ai-film-api，两者不可互换。
CAPABILITY_AUDIENCE = "ai-film-capability"

#: 项目范围通配符。与 Principal 的 ``*`` 语义一致：本组织内全部项目。
CAPABILITY_PROJECT_ALL = "*"

# 标准 claim
_CLAIM_ISSUER = "iss"
_CLAIM_AUDIENCE = "aud"
_CLAIM_SUBJECT = "sub"
_CLAIM_EXPIRES = "exp"
_CLAIM_ISSUED = "iat"
_CLAIM_NOT_BEFORE = "nbf"
_CLAIM_JTI = "jti"

# 自定义 claim（与 §C.5.1 一一对应，名字即契约）
_CLAIM_TENANT = "tenant_id"
_CLAIM_ORGANIZATION = "org_id"
_CLAIM_PROJECT_SCOPE = "project_scope"
_CLAIM_EPISODE_SCOPE = "episode_scope"
_CLAIM_SHOT_SCOPE = "shot_scope"
_CLAIM_ASSET_SCOPE = "asset_scope"
_CLAIM_ALLOWED_ACTIONS = "allowed_actions"
_CLAIM_DENIED_ACTIONS = "denied_actions"
_CLAIM_BUDGET_SCOPE = "budget_scope"
_CLAIM_MAX_AMOUNT = "max_amount"
_CLAIM_CURRENCY = "currency"
_CLAIM_RESOURCE_QUOTA = "resource_quota"
_CLAIM_DELEGATION_CHAIN = "delegation_chain"
_CLAIM_APPROVAL_REF = "approval_ref"
_CLAIM_REVOCATION_REF = "revocation_ref"
_CLAIM_POLICY_VERSION = "policy_version"
_CLAIM_NONCE = "nonce"
_CLAIM_TASK_ID = "task_id"
_CLAIM_WORKFLOW_RUN_ID = "workflow_run_id"

_MAX_TTL_SECONDS = 900


class CapabilityError(ValueError):
    """Capability Token 验签或 claim 非法。"""


class CapabilityExpiredError(CapabilityError):
    """Capability Token 已过期（与验签失败区分，便于审计计数）。"""


@dataclass(frozen=True, slots=True)
class Capability:
    """验签成功后的 capability 值对象（不可变，可直接进审计）。"""

    jti: str
    subject_id: str
    tenant_id: str
    organization_id: str
    issuer: str
    project_scope: frozenset[str]
    episode_scope: frozenset[str]
    shot_scope: frozenset[str]
    asset_scope: frozenset[str]
    allowed_actions: frozenset[str]
    denied_actions: frozenset[str]
    budget_scope: frozenset[str]
    max_amount: str | None
    currency: str | None
    resource_quota: tuple[tuple[str, int], ...]
    delegation_chain: tuple[str, ...]
    approval_ref: str | None
    revocation_ref: str | None
    policy_version: str
    nonce: str
    task_id: str
    workflow_run_id: str
    issued_at: datetime
    not_before: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        for field_name in (
            "jti",
            "subject_id",
            "tenant_id",
            "organization_id",
            "issuer",
            "nonce",
            "task_id",
            "policy_version",
        ):
            if not str(getattr(self, field_name)).strip():
                raise CapabilityError(f"Capability {field_name} 不能为空")
        if self.not_before < self.issued_at:
            raise CapabilityError("Capability not_before 早于 issued_at")
        if self.expires_at <= self.not_before:
            raise CapabilityError("Capability expires_at 必须晚于 not_before")
        if not self.allowed_actions:
            raise CapabilityError("Capability 必须声明 allowed_actions")
        if self.allowed_actions & self.denied_actions:
            raise CapabilityError("Capability 的 allowed/denied 动作不能重叠")
        if any(not item.strip() for item in self.delegation_chain):
            raise CapabilityError("Capability delegation_chain 含空主体")
        if len(set(self.delegation_chain)) != len(self.delegation_chain):
            raise CapabilityError("Capability delegation_chain 不得重复（防环）")
        if self.max_amount is not None:
            try:
                if Decimal(self.max_amount) < 0:
                    raise CapabilityError("Capability max_amount 不能为负")
            except InvalidOperation as exc:
                raise CapabilityError("Capability max_amount 不是合法金额") from exc
        if any(value < 0 for _, value in self.resource_quota):
            raise CapabilityError("Capability resource_quota 不能为负")

    def is_active_at(self, moment: datetime) -> bool:
        """按 not_before / expires_at 判断有效窗口（闭开区间）。"""
        return self.not_before <= moment < self.expires_at

    def allows_action(self, action_key: str) -> bool:
        """动作必须在 allow 列表里，且不在 deny 列表里。"""
        return action_key in self.allowed_actions and action_key not in self.denied_actions

    def allows_project(self, project_id: str, *, principal_has_all_projects: bool) -> bool:
        """capability 只能收窄，不能扩大 principal 的项目范围。

        capability 带 ``*`` 但 principal 本身不是组织级范围时按拒绝处理；
        这是"不可扩大 scope"的直接执行点，不依赖调用方自觉。
        """
        if CAPABILITY_PROJECT_ALL in self.project_scope:
            return principal_has_all_projects
        return project_id in self.project_scope

    def to_audit_dict(self) -> dict[str, object]:
        """审计结构：只含 jti 与范围，不含令牌原文。"""
        return {
            "capability_jti": self.jti,
            "subject_id": self.subject_id,
            "tenant_id": self.tenant_id,
            "organization_id": self.organization_id,
            "issuer": self.issuer,
            "task_id": self.task_id,
            "workflow_run_id": self.workflow_run_id,
            "project_scope": sorted(self.project_scope),
            "allowed_actions": sorted(self.allowed_actions),
            "denied_actions": sorted(self.denied_actions),
            "policy_version": self.policy_version,
            "approval_ref": self.approval_ref,
            "revocation_ref": self.revocation_ref,
            "delegation_depth": len(self.delegation_chain),
            "expires_at": self.expires_at.isoformat(),
        }


def _string_list(claims: dict[str, Any], name: str, *, required: bool = True) -> frozenset[str]:
    raw = claims.get(name)
    if raw is None and not required:
        return frozenset()
    if not isinstance(raw, list) or any(not isinstance(item, str) or not item.strip() for item in raw):
        raise CapabilityError(f"Capability {name} 必须是非空字符串数组")
    return frozenset(raw)


def _optional_string(claims: dict[str, Any], name: str) -> str | None:
    value = claims.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise CapabilityError(f"Capability {name} 必须是非空字符串")
    return value


def _timestamp(value: Any, name: str) -> datetime:
    if not isinstance(value, int):
        raise CapabilityError(f"Capability {name} 必须是 Unix 秒整数")
    return datetime.fromtimestamp(value, UTC)


def decode_capability(
    token: str,
    *,
    public_pem: bytes,
    issuer: str,
    audience: str = CAPABILITY_AUDIENCE,
    leeway_seconds: int = 0,
) -> Capability:
    """验签并解析 capability；任何异常都收敛成 CapabilityError。"""
    try:
        claims = jwt.decode(
            token,
            public_pem,
            algorithms=[ALGORITHM],
            issuer=issuer,
            audience=audience,
            leeway=leeway_seconds,
            options={
                "require": [
                    _CLAIM_ISSUER,
                    _CLAIM_AUDIENCE,
                    _CLAIM_SUBJECT,
                    _CLAIM_EXPIRES,
                    _CLAIM_ISSUED,
                    _CLAIM_JTI,
                    _CLAIM_TENANT,
                    _CLAIM_ORGANIZATION,
                    _CLAIM_ALLOWED_ACTIONS,
                ]
            },
        )
    except ExpiredSignatureError as exc:
        raise CapabilityExpiredError("Capability Token 已过期") from exc
    except InvalidTokenError as exc:
        raise CapabilityError(f"Capability Token 验签失败: {exc}") from exc

    if not isinstance(claims, dict):
        raise CapabilityError("Capability claims 必须是对象")

    subject = claims.get(_CLAIM_SUBJECT)
    tenant = claims.get(_CLAIM_TENANT)
    organization = claims.get(_CLAIM_ORGANIZATION)
    jti = claims.get(_CLAIM_JTI)
    if any(not isinstance(item, str) or not item.strip() for item in (subject, tenant, organization, jti)):
        raise CapabilityError("Capability 缺少合法的 sub/tenant/org/jti")

    raw_quota = claims.get(_CLAIM_RESOURCE_QUOTA, {})
    if not isinstance(raw_quota, dict) or any(
        not isinstance(key, str)
        or not key.strip()
        or isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        for key, value in raw_quota.items()
    ):
        raise CapabilityError("Capability resource_quota 必须是 字符串->非负整数 映射")

    chain = claims.get(_CLAIM_DELEGATION_CHAIN, [])
    if not isinstance(chain, list) or any(not isinstance(item, str) or not item.strip() for item in chain):
        raise CapabilityError("Capability delegation_chain 必须是非空字符串数组")

    issued_at = _timestamp(claims[_CLAIM_ISSUED], _CLAIM_ISSUED)
    not_before_raw = claims.get(_CLAIM_NOT_BEFORE, claims[_CLAIM_ISSUED])
    not_before = _timestamp(not_before_raw, _CLAIM_NOT_BEFORE)
    expires_at = _timestamp(claims[_CLAIM_EXPIRES], _CLAIM_EXPIRES)

    return Capability(
        jti=cast(str, jti),
        subject_id=cast(str, subject),
        tenant_id=cast(str, tenant),
        organization_id=cast(str, organization),
        issuer=str(claims[_CLAIM_ISSUER]),
        project_scope=_string_list(claims, _CLAIM_PROJECT_SCOPE, required=False),
        episode_scope=_string_list(claims, _CLAIM_EPISODE_SCOPE, required=False),
        shot_scope=_string_list(claims, _CLAIM_SHOT_SCOPE, required=False),
        asset_scope=_string_list(claims, _CLAIM_ASSET_SCOPE, required=False),
        allowed_actions=_string_list(claims, _CLAIM_ALLOWED_ACTIONS),
        denied_actions=_string_list(claims, _CLAIM_DENIED_ACTIONS, required=False),
        budget_scope=_string_list(claims, _CLAIM_BUDGET_SCOPE, required=False),
        max_amount=_optional_string(claims, _CLAIM_MAX_AMOUNT),
        currency=_optional_string(claims, _CLAIM_CURRENCY),
        resource_quota=tuple(sorted(raw_quota.items())),
        delegation_chain=tuple(chain),
        approval_ref=_optional_string(claims, _CLAIM_APPROVAL_REF),
        revocation_ref=_optional_string(claims, _CLAIM_REVOCATION_REF),
        policy_version=str(claims.get(_CLAIM_POLICY_VERSION, "")),
        nonce=str(claims.get(_CLAIM_NONCE, "")),
        task_id=str(claims.get(_CLAIM_TASK_ID, "")),
        workflow_run_id=str(claims.get(_CLAIM_WORKFLOW_RUN_ID, "")),
        issued_at=issued_at,
        not_before=not_before,
        expires_at=expires_at,
    )


@dataclass(frozen=True, slots=True)
class CapabilityIssuer:
    """Authorization Service 的签发端（显式持私钥；API 进程不得构造）。"""

    signing_key: SigningKey
    issuer: str
    audience: str = CAPABILITY_AUDIENCE
    ttl_seconds: int = 300

    def __post_init__(self) -> None:
        if self.ttl_seconds <= 0 or self.ttl_seconds > _MAX_TTL_SECONDS:
            raise CapabilityError(f"Capability TTL 必须在 (0, {_MAX_TTL_SECONDS}] 秒内")

    def issue(
        self,
        *,
        subject_id: str,
        tenant_id: str,
        organization_id: str,
        allowed_actions: list[str],
        project_scope: list[str],
        task_id: str,
        workflow_run_id: str,
        policy_version: str,
        denied_actions: list[str] | None = None,
        episode_scope: list[str] | None = None,
        shot_scope: list[str] | None = None,
        asset_scope: list[str] | None = None,
        budget_scope: list[str] | None = None,
        max_amount: str | None = None,
        currency: str | None = None,
        resource_quota: dict[str, int] | None = None,
        delegation_chain: list[str] | None = None,
        approval_ref: str | None = None,
        revocation_ref: str | None = None,
        ttl_seconds: int | None = None,
    ) -> str:
        """签发一条短时 capability；scope 由调用方显式给出，缺省即空集。"""
        ttl = self.ttl_seconds if ttl_seconds is None else ttl_seconds
        if ttl <= 0 or ttl > _MAX_TTL_SECONDS:
            raise CapabilityError(f"Capability TTL 必须在 (0, {_MAX_TTL_SECONDS}] 秒内")
        now = datetime.now(UTC)
        claims: dict[str, Any] = {
            _CLAIM_ISSUER: self.issuer,
            _CLAIM_AUDIENCE: self.audience,
            _CLAIM_SUBJECT: subject_id,
            _CLAIM_TENANT: tenant_id,
            _CLAIM_ORGANIZATION: organization_id,
            _CLAIM_ISSUED: int(now.timestamp()),
            _CLAIM_NOT_BEFORE: int(now.timestamp()),
            _CLAIM_EXPIRES: int((now + timedelta(seconds=ttl)).timestamp()),
            _CLAIM_JTI: uuid.uuid4().hex,
            _CLAIM_NONCE: uuid.uuid4().hex,
            _CLAIM_ALLOWED_ACTIONS: list(allowed_actions),
            _CLAIM_DENIED_ACTIONS: list(denied_actions or []),
            _CLAIM_PROJECT_SCOPE: list(project_scope),
            _CLAIM_EPISODE_SCOPE: list(episode_scope or []),
            _CLAIM_SHOT_SCOPE: list(shot_scope or []),
            _CLAIM_ASSET_SCOPE: list(asset_scope or []),
            _CLAIM_BUDGET_SCOPE: list(budget_scope or []),
            _CLAIM_RESOURCE_QUOTA: dict(resource_quota or {}),
            _CLAIM_DELEGATION_CHAIN: list(delegation_chain or []),
            _CLAIM_APPROVAL_REF: approval_ref,
            _CLAIM_REVOCATION_REF: revocation_ref,
            _CLAIM_POLICY_VERSION: policy_version,
            _CLAIM_TASK_ID: task_id,
            _CLAIM_WORKFLOW_RUN_ID: workflow_run_id,
        }
        if max_amount is not None:
            claims[_CLAIM_MAX_AMOUNT] = str(Decimal(max_amount))
        if currency is not None:
            claims[_CLAIM_CURRENCY] = currency
        return jwt.encode(
            claims,
            self.signing_key.private_pem,
            algorithm=ALGORITHM,
            headers={"kid": self.signing_key.kid},
        )


__all__ = [
    "CAPABILITY_AUDIENCE",
    "CAPABILITY_PROJECT_ALL",
    "Capability",
    "CapabilityError",
    "CapabilityExpiredError",
    "CapabilityIssuer",
    "decode_capability",
]
