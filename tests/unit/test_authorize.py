"""统一授权决策入口（authorize）+ 职责分离抽象的单元测试。

纯离线（不依赖 PostgreSQL），验证 F02 根因治理的真实行为：
- RBAC（角色求交）与 Capability（token_required 强制校验）是两条独立
  授权链，缺一即拒绝（fail-closed）。
- 职责分离统一判定：主体缺失 / 自批自锁 / 正常放行三态。
"""

from __future__ import annotations

import uuid

import pytest

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.identity.principal import Principal
from packages.identity.separation import require_different_subject
from services.identity_access.rbac import authorize

_TENANT = uuid.UUID("11111111-2222-3333-4444-555555555555")


def _principal(
    roles: set[str] | None = None,
    capabilities: set[str] | None = None,
    subject_id: str = "user-001",
) -> Principal:
    return Principal(
        subject_id=subject_id,
        tenant_id=_TENANT,
        roles=frozenset(roles or set()),
        capabilities=frozenset(capabilities or set()),
    )


# --------------------------------------------------------------------------- #
# RBAC + Capability 统一授权链
# --------------------------------------------------------------------------- #
def test_authorize_allows_role_plus_capability() -> None:
    """角色命中 + 能力覆盖（token_required=true）→ 放行。"""
    p = _principal(roles={"项目管理员"}, capabilities={"Project:*"})
    assert authorize("PROJECT.CREATE", p) is p


def test_authorize_rejects_missing_role_even_with_capability() -> None:
    """能力覆盖但角色不匹配 → 拒绝（RBAC 是独立前提）。"""
    p = _principal(roles={"编剧"}, capabilities={"Project:*"})
    with pytest.raises(DomainError) as exc_info:
        authorize("PROJECT.CREATE", p)
    assert exc_info.value.error_code is ErrorRegistry.AUTH_SCOPE_DENIED


def test_authorize_rejects_missing_capability_even_with_role() -> None:
    """角色命中但能力缺失（token_required=true）→ 拒绝（Capability 是独立前提）。"""
    p = _principal(roles={"项目管理员"}, capabilities=set())
    with pytest.raises(DomainError) as exc_info:
        authorize("PROJECT.CREATE", p)
    assert exc_info.value.error_code is ErrorRegistry.AUTH_SCOPE_DENIED
    assert "能力范围" in exc_info.value.detail


def test_authorize_accepts_exact_action_capability() -> None:
    """能力以精确 action_key 表达 → 放行。"""
    p = _principal(roles={"项目管理员"}, capabilities={"PROJECT.CREATE"})
    assert authorize("PROJECT.CREATE", p) is p


def test_authorize_accepts_wildcard_capability() -> None:
    """能力以顶层通配 ``*`` 表达 → 放行（受信内部主体）。"""
    p = _principal(roles={"项目管理员"}, capabilities={"*"})
    assert authorize("PROJECT.CREATE", p) is p


def test_authorize_rejects_irrelevant_capability() -> None:
    """能力集存在但与目标动作无关 → 拒绝（fail-closed，不静默放行）。"""
    p = _principal(roles={"项目管理员"}, capabilities={"Episode:*"})
    with pytest.raises(DomainError) as exc_info:
        authorize("PROJECT.CREATE", p)
    assert exc_info.value.error_code is ErrorRegistry.AUTH_SCOPE_DENIED


def test_authorize_rejects_unknown_action() -> None:
    """未注册 action_key → 结构化内部错误（fail-fast，非静默放行）。"""
    p = _principal(roles={"项目管理员"}, capabilities={"*"})
    with pytest.raises(DomainError) as exc_info:
        authorize("NO.SUCH.ACTION", p)
    assert exc_info.value.error_code is ErrorRegistry.INTERNAL_ERROR


# --------------------------------------------------------------------------- #
# 职责分离统一判定（require_different_subject）
# --------------------------------------------------------------------------- #
def test_separation_allows_different_subjects() -> None:
    """申请人 ≠ 批准人 → 放行（无异常）。"""
    require_different_subject(
        actor="director-001",
        counterpart="writer-001",
        action_label="SCRIPT.LOCK.APPROVE",
    )


def test_separation_rejects_self_approval() -> None:
    """申请人 == 批准人（自批/自锁）→ 拒绝。"""
    with pytest.raises(DomainError) as exc_info:
        require_different_subject(
            actor="writer-001",
            counterpart="writer-001",
            action_label="SCRIPT.LOCK.APPROVE",
        )
    assert exc_info.value.error_code is ErrorRegistry.STATE_TRANSITION_DENIED


def test_separation_rejects_missing_counterpart() -> None:
    """缺少申请人（审批链不完整）→ 拒绝（不可静默绕过比对）。"""
    with pytest.raises(DomainError) as exc_info:
        require_different_subject(
            actor="director-001",
            counterpart=None,
            action_label="SCRIPT.LOCK.APPROVE",
        )
    assert exc_info.value.error_code is ErrorRegistry.STATE_TRANSITION_DENIED


def test_separation_rejects_empty_actor() -> None:
    """当前主体为空 → 拒绝（防御性）。"""
    with pytest.raises(DomainError) as exc_info:
        require_different_subject(
            actor="   ",
            counterpart="writer-001",
            action_label="SCRIPT.LOCK.APPROVE",
        )
    assert exc_info.value.error_code is ErrorRegistry.STATE_TRANSITION_DENIED


def test_separation_whitespace_insensitive() -> None:
    """主体比对忽略首尾空白（避免 'a' vs 'a ' 绕过红线）。"""
    with pytest.raises(DomainError):
        require_different_subject(
            actor="writer-001",
            counterpart="  writer-001  ",
            action_label="SCRIPT.LOCK.APPROVE",
        )


# --------------------------------------------------------------------------- #
# 归属解析的覆盖度（结构守卫，纯离线）
# --------------------------------------------------------------------------- #
class _EmptyResult:
    """空结果集：resolver 走「查不到 → None」分支，不触碰数据库。"""

    def first(self) -> None:
        return None


class _EmptySession:
    """只实现 resolver 用到的 execute，用于离线验证「类型是否登记」。"""

    async def execute(self, statement: object, parameters: dict[str, str]) -> _EmptyResult:
        return _EmptyResult()


async def test_ownership_resolver_registers_organization() -> None:
    """归属链根节点（Organization）必须可解析，否则创建路径只能自证归属。

    回归背景：``_RESOLVE_SQL`` 曾只登记 Project/Episode/ScriptRevision/
    AssetVersion/QCReport，漏了组织这一层。于是「创建时指定组织」无法用数据库
    事实确认「该组织是否在本租户内」，只能拿 principal 的租户拼一条必然自洽的
    路径——跨租户创建于是要等外键报错才被发现，并被映射成 409「并发修改」，
    把「越权/不可见」误报成「重试即可」。
    """
    from packages.identity.ownership import SqlOwnershipResolver

    resolver = SqlOwnershipResolver(_EmptySession())  # type: ignore[arg-type]
    # 未登记的类型会抛 INTERNAL_ERROR（fail-closed），因此返回 None 即证明已登记。
    assert await resolver.resolve("Organization", str(uuid.uuid4())) is None
