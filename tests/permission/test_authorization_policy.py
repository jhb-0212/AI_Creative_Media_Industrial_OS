"""授权决策规则测试（包 B2）。

两类用例：

1. **规则用例**：每条规则一个，输入输出都是纯数据（无数据库、无 HTTP）。
   拒绝理由码逐个钉死——理由码是审计与告警的接口，改名会让历史日志无法聚合。
2. **契约穷举**：对 Action Registry 里**全部** 81 条动作跑一遍，证明
   「没有哪个动作对无角色主体放行」「没有哪个 CRITICAL 动作对 Agent 放行」。
   抽样测试证明不了这两点：漏掉的那条恰好就是漏洞。
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.identity.action_registry import all_action_specs
from packages.identity.authorization import (
    ActionSpec,
    DecisionEffect,
    DecisionReason,
    ResourceRef,
    RiskLevel,
    ScopeDimension,
    decide,
    parse_scope,
)
from packages.identity.capability import Capability
from packages.identity.enforcement import authorize
from packages.identity.principal import Principal, SubjectType

_TENANT = uuid.UUID("00000000-0000-0000-0000-0000000000aa")
_ORG_A = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
_ORG_B = uuid.UUID("00000000-0000-0000-0000-0000000000b1")

_TEST_ACTIONS = frozenset(
    {spec.action_key for spec in all_action_specs()} | {"TEST.ACTION"}
)


def _capability(
    *,
    subject: str = "user-1",
    tenant: uuid.UUID = _TENANT,
    org: uuid.UUID = _ORG_A,
    actions: frozenset[str] = _TEST_ACTIONS,
    projects: frozenset[str] = frozenset({"*"}),
) -> Capability:
    """直接构造 capability 值对象（绕开签名，专测授权决策的消费逻辑）。"""
    now = datetime.now(UTC)
    return Capability(
        jti=f"cap-{subject}-{uuid.uuid4().hex[:8]}",
        subject_id=subject,
        tenant_id=str(tenant),
        organization_id=str(org),
        issuer="authz-service",
        project_scope=projects,
        episode_scope=frozenset(),
        shot_scope=frozenset(),
        asset_scope=frozenset(),
        allowed_actions=actions,
        denied_actions=frozenset(),
        budget_scope=frozenset(),
        max_amount=None,
        currency=None,
        resource_quota=(),
        delegation_chain=(),
        approval_ref=None,
        revocation_ref=None,
        policy_version="V205-R14",
        nonce=uuid.uuid4().hex,
        task_id="task-1",
        workflow_run_id="run-1",
        issued_at=now,
        not_before=now,
        expires_at=now + timedelta(minutes=5),
    )


def _principal(
    *roles: str,
    org: uuid.UUID = _ORG_A,
    projects: frozenset[str] = frozenset({"*"}),
    subject: str = "user-1",
    subject_type: SubjectType = SubjectType.HUMAN,
    with_capability: bool = True,
    capability: Capability | None = None,
) -> Principal:
    return Principal(
        subject_id=subject,
        tenant_id=_TENANT,
        organization_id=org,
        subject_type=subject_type,
        roles=frozenset(roles),
        project_ids=projects,
        capability=(
            capability
            if capability is not None
            else _capability(subject=subject, tenant=_TENANT, org=org)
            if with_capability
            else None
        ),
    )


def _spec(
    *,
    action_key: str = "TEST.ACTION",
    scope: str = "project/asset",
    risk_level: RiskLevel = RiskLevel.MEDIUM,
    principals: frozenset[str] = frozenset({"导演"}),
    principals_text: str = "导演",
) -> ActionSpec:
    return ActionSpec(
        action_key=action_key,
        resource_type="AssetVersion",
        scope=scope,
        risk_level=risk_level,
        allowed_principals=principals,
        token_required=True,
        approval_requirement="",
        allowed_principals_text=principals_text,
    )


# --------------------------------------------------------------------------- #
# 规则用例
# --------------------------------------------------------------------------- #
def test_unregistered_action_is_denied() -> None:
    """未注册动作：拒绝，且按最高风险等级上报（否则审计里会被当噪声忽略）。"""
    decision = decide(_principal("导演"), None)
    assert decision.effect is DecisionEffect.DENY
    assert decision.reason is DecisionReason.ACTION_UNREGISTERED
    assert decision.risk_level is RiskLevel.CRITICAL


def test_empty_allowed_principals_is_denied() -> None:
    """契约未声明允许主体 ≠ 谁都可以。"""
    decision = decide(_principal("导演"), _spec(principals=frozenset(), principals_text=""))
    assert decision.reason is DecisionReason.NO_ALLOWED_PRINCIPAL


def test_joint_approval_is_rejected_not_split() -> None:
    """``A+B`` 是共同审批（AND），不是二选一（OR）；未实现联合审批前一律拒绝。"""
    spec = _spec(
        action_key="COMMUNITY.REUSE.APPROVE",
        principals=frozenset({"源权利人+目标项目负责人+相关职责审批"}),
        principals_text="源权利人+目标项目负责人+相关职责审批",
        risk_level=RiskLevel.CRITICAL,
    )
    decision = decide(
        _principal("源权利人+目标项目负责人+相关职责审批"),
        spec,
        applicant_subject_id="someone-else",
    )
    assert decision.reason is DecisionReason.JOINT_APPROVAL_UNSUPPORTED


def test_role_mismatch_is_denied() -> None:
    decision = decide(_principal("编剧"), _spec())
    assert decision.reason is DecisionReason.ROLE_MISMATCH


def test_agent_cannot_execute_critical_action() -> None:
    """红线「Agent 不得自批自用」：Agent 角色匹配也不能过 CRITICAL。"""
    decision = decide(
        _principal("导演", subject_type=SubjectType.AGENT),
        _spec(risk_level=RiskLevel.CRITICAL),
    )
    assert decision.reason is DecisionReason.AGENT_CRITICAL_FORBIDDEN


def test_agent_can_execute_non_critical_action() -> None:
    """非 CRITICAL 动作不因主体是 Agent 被拒——否则 Worker/Adapter 无法工作。"""
    decision = decide(
        _principal("导演", subject_type=SubjectType.AGENT),
        _spec(risk_level=RiskLevel.HIGH),
        resource=ResourceRef("AssetVersion", _ORG_A, "proj-1"),
    )
    assert decision.effect is DecisionEffect.ALLOW


@pytest.mark.agent_negative("AGENT-NEG-001")
def test_self_approval_is_denied() -> None:
    decision = decide(
        _principal("导演", subject="user-1"),
        _spec(action_key="ASSET.VERSION.APPROVE", risk_level=RiskLevel.HIGH),
        resource=ResourceRef("AssetVersion", _ORG_A, "proj-1"),
        applicant_subject_id="user-1",
    )
    assert decision.reason is DecisionReason.SELF_APPROVAL_FORBIDDEN


def test_approval_without_applicant_is_recorded_as_deferred_check() -> None:
    """依赖阶段拿不到申请人 → 记为 deferred（不是静默跳过，也不是 403）。"""
    decision = decide(
        _principal("导演"),
        _spec(action_key="ASSET.VERSION.APPROVE", risk_level=RiskLevel.HIGH),
    )
    assert decision.allowed
    assert "separation_of_duties" in decision.deferred_checks
    assert decision.reason is DecisionReason.SCOPE_DEFERRED_TO_RLS


def test_cross_organization_is_denied() -> None:
    decision = decide(
        _principal("导演", org=_ORG_A),
        _spec(scope="tenant+organization"),
        resource=ResourceRef("Project", _ORG_B, "proj-1"),
    )
    assert decision.reason is DecisionReason.ORGANIZATION_MISMATCH


def test_resource_without_organization_is_denied_not_assumed() -> None:
    """资源存在但组织未知 → 无法判定即拒绝，绝不"当作同组织"。"""
    decision = decide(
        _principal("导演"),
        _spec(scope="tenant+organization"),
        resource=ResourceRef("Project", None, "proj-1"),
    )
    assert decision.reason is DecisionReason.RESOURCE_UNRESOLVED


def test_project_out_of_scope_is_denied() -> None:
    decision = decide(
        _principal("导演", projects=frozenset({"proj-a"})),
        _spec(scope="project"),
        resource=ResourceRef("AssetVersion", _ORG_A, "proj-b"),
    )
    assert decision.reason is DecisionReason.PROJECT_SCOPE_DENIED


def test_empty_project_scope_denies_everything() -> None:
    """空范围 = 一个项目也访问不了（fail-closed），不做"没声明就放行"。"""
    decision = decide(
        _principal("导演", projects=frozenset()),
        _spec(scope="project"),
        resource=ResourceRef("AssetVersion", _ORG_A, "proj-a"),
    )
    assert decision.reason is DecisionReason.PROJECT_SCOPE_DENIED


def test_unresolved_resource_defers_dimensions_to_rls() -> None:
    """列表/按 id 读取：范围维度显式记为 deferred，而不是假装检查过。"""
    decision = decide(
        _principal("导演"),
        _spec(scope="tenant/organization/project"),
    )
    assert decision.allowed
    assert decision.reason is DecisionReason.SCOPE_DEFERRED_TO_RLS
    assert set(decision.deferred_dimensions) == {
        ScopeDimension.ORGANIZATION,
        ScopeDimension.PROJECT,
    }
    assert decision.checked_dimensions == ()


def test_resolved_resource_records_checked_dimensions() -> None:
    decision = decide(
        _principal("导演"),
        _spec(scope="organization/project"),
        resource=ResourceRef("AssetVersion", _ORG_A, "proj-1"),
    )
    assert decision.reason is DecisionReason.ROLE_AND_SCOPE_VERIFIED
    assert set(decision.checked_dimensions) == {
        ScopeDimension.ORGANIZATION,
        ScopeDimension.PROJECT,
    }
    assert decision.deferred_dimensions == ()


def test_audit_dict_never_contains_token_or_secret() -> None:
    """审计结构只含主体标识与理由码（红线「密钥不落日志」）。"""
    decision = decide(_principal("导演"), _spec())
    payload = decision.to_audit_dict()
    assert set(payload) == {
        "action_key",
        "effect",
        "reason",
        "detail",
        "subject_id",
        "subject_type",
        "risk_level",
        "checked",
        "deferred",
        "deferred_checks",
        "capability_jti",
    }


# --------------------------------------------------------------------------- #
# Capability Token（F3）：同一个 decide() 入口消费短时能力凭证
# --------------------------------------------------------------------------- #
def test_capability_missing_is_denied() -> None:
    """契约要求 token，缺 capability 一律拒绝（不能用访问令牌顶替）。"""
    decision = decide(_principal("导演", with_capability=False), _spec())
    assert decision.reason is DecisionReason.CAPABILITY_MISSING


def test_capability_subject_mismatch_is_denied() -> None:
    """capability 绑定的主体必须与已验签访问令牌一致。"""
    decision = decide(
        _principal("导演", capability=_capability(subject="someone-else")),
        _spec(),
    )
    assert decision.reason is DecisionReason.CAPABILITY_SUBJECT_MISMATCH


def test_capability_tenant_and_organization_mismatch_is_denied() -> None:
    """跨租户/跨组织复用 capability 必须被拒（ABAC 第一层）。"""
    cross_tenant = decide(
        _principal(
            "导演",
            capability=_capability(tenant=uuid.UUID("00000000-0000-0000-0000-0000000000ff")),
        ),
        _spec(),
    )
    cross_org = decide(
        _principal("导演", capability=_capability(org=_ORG_B)),
        _spec(),
    )
    assert cross_tenant.reason is DecisionReason.CAPABILITY_TENANT_MISMATCH
    assert cross_org.reason is DecisionReason.CAPABILITY_ORGANIZATION_MISMATCH


def test_capability_action_scope_is_denied() -> None:
    """capability 未授权的动作不能执行；deny 列表优先于 allow 列表。"""
    missing = decide(
        _principal("导演", capability=_capability(actions=frozenset({"OTHER.ACTION"}))),
        _spec(),
    )
    assert missing.reason is DecisionReason.CAPABILITY_ACTION_DENIED

    denied_capability = replace(
        _capability(actions=frozenset({"OTHER.ACTION"})),
        denied_actions=frozenset({"TEST.ACTION"}),
    )
    denied = decide(_principal("导演", capability=denied_capability), _spec())
    assert denied.reason is DecisionReason.CAPABILITY_ACTION_DENIED


def test_capability_project_scope_is_denied() -> None:
    """同租户跨项目：principal 能进但 capability 没授权的项目仍拒绝。"""
    narrow = _capability(projects=frozenset({"proj-a"}))
    decision = decide(
        _principal("导演", capability=narrow),
        _spec(),
        resource=ResourceRef("AssetVersion", _ORG_A, "proj-b"),
    )
    assert decision.reason is DecisionReason.CAPABILITY_PROJECT_SCOPE_DENIED


def test_capability_wildcard_cannot_expand_principal_scope() -> None:
    """capability 带 ``*`` 也不能把项目级主体放大成组织级主体。"""
    decision = decide(
        _principal("导演", projects=frozenset({"proj-a"})),
        _spec(),
        resource=ResourceRef("AssetVersion", _ORG_A, "proj-a"),
    )
    assert decision.reason is DecisionReason.CAPABILITY_PROJECT_SCOPE_DENIED


# --------------------------------------------------------------------------- #
# scope 解析
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "scope, expected",
    [
        ("tenant+organization", {ScopeDimension.TENANT, ScopeDimension.ORGANIZATION}),
        ("project/shot/asset", {ScopeDimension.PROJECT}),
        ("tenant/organization/project", {
            ScopeDimension.TENANT,
            ScopeDimension.ORGANIZATION,
            ScopeDimension.PROJECT,
        }),
        # 社区/创作侧的作用域不含三个可判定维度：判定上不放行范围检查，
        # 由各自的隔离模型处理（当前这些动作尚未接入路由）。
        ("public/post", set()),
    ],
)
def test_parse_scope_extracts_only_decidable_dimensions(
    scope: str, expected: set[ScopeDimension]
) -> None:
    assert set(parse_scope(scope)) == expected


# --------------------------------------------------------------------------- #
# 契约穷举（不是抽样）
# --------------------------------------------------------------------------- #
def test_no_action_is_open_to_principal_without_roles() -> None:
    """81 条动作逐个验证：无角色主体不可能被放行。

    这条比"某几个动作测过"强得多：新增动作若把 allowed_principals 写成空、
    或写成只有 Agent 能匹配的主体，都会在这里暴露（前者 NO_ALLOWED_PRINCIPAL，
    后者 ROLE_MISMATCH）。
    """
    anonymous = _principal()
    leaked = [
        spec.action_key
        for spec in all_action_specs()
        if decide(anonymous, spec).effect is DecisionEffect.ALLOW
    ]
    assert leaked == [], f"以下动作对无角色主体放行: {leaked}"


@pytest.mark.agent_negative("AGENT-NEG-001")
def test_no_critical_action_is_executable_by_agent() -> None:
    """10 条 CRITICAL 动作逐个验证：角色完全匹配的 Agent 也必须被拒。"""
    violations: list[str] = []
    for spec in all_action_specs():
        if not spec.is_critical:
            continue
        agent = _principal(
            *spec.allowed_principals, subject_type=SubjectType.AGENT
        )
        decision = decide(
            agent,
            spec,
            resource=ResourceRef(spec.resource_type, _ORG_A, "proj-1"),
            applicant_subject_id="creator-1",
        )
        if decision.effect is not DecisionEffect.DENY:
            violations.append(spec.action_key)
        elif decision.reason is not DecisionReason.AGENT_CRITICAL_FORBIDDEN:
            # 可能是先被"联合审批"或"角色"拦下——那也是拒绝，但理由不同。
            assert decision.reason in {
                DecisionReason.AGENT_CRITICAL_FORBIDDEN,
                DecisionReason.JOINT_APPROVAL_UNSUPPORTED,
                DecisionReason.ROLE_MISMATCH,
            }, (spec.action_key, decision.reason)
    assert violations == [], f"以下 CRITICAL 动作可被 Agent 执行: {violations}"


def test_every_contract_action_has_parsable_scope_and_known_risk() -> None:
    """契约自检：scope 可解析、风险等级已登记、action_key 唯一。"""
    keys = [spec.action_key for spec in all_action_specs()]
    assert len(keys) == len(set(keys))
    for spec in all_action_specs():
        assert isinstance(spec.risk_level, RiskLevel)
        assert spec.scope.strip(), spec.action_key
        parse_scope(spec.scope)


# --------------------------------------------------------------------------- #
# 强制执行点（拒绝必须是异常，不是"返回一个待检查的对象"）
# --------------------------------------------------------------------------- #
def test_authorize_raises_domain_error_on_deny() -> None:
    """拒绝必须抛 403 信封：调用方"忘了检查返回值"不能变成静默放行。"""
    with pytest.raises(DomainError) as exc:
        authorize(_principal("编剧"), "ASSET.VERSION.APPROVE", applicant_subject_id="x")
    assert exc.value.error_code is ErrorRegistry.AUTH_SCOPE_DENIED
    # 理由码必须出现在错误详情里，前端与审计才能区分"没角色"和"跨组织"。
    assert "ROLE_MISMATCH" in (exc.value.detail or "")


def test_agent_is_blocked_by_enforcement_path_on_critical_action() -> None:
    """走完整的强制执行点（不是只测纯函数）：CRITICAL 动作对 Agent 抛 403。

    契约里有 10 条 CRITICAL 动作，但当前**没有任何一条接入了 HTTP 路由**，
    因此这条规则只能在策略层证明。等主链接入交付/放行动作后，应有对应的
    接口级拒绝测试（见 ADR-R18「残留风险」）。
    """
    agent = _principal("授权发行主体", subject_type=SubjectType.AGENT)
    with pytest.raises(DomainError) as exc:
        authorize(
            agent,
            "DELIVERY.MANIFEST.RELEASE",
            resource=ResourceRef("DeliveryManifest", _ORG_A, "proj-1"),
            applicant_subject_id="creator-1",
        )
    assert "AGENT_CRITICAL_FORBIDDEN" in (exc.value.detail or "")
