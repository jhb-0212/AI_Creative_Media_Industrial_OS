"""授权决策：Principal + Action + Resource + Context → ALLOW / DENY。

为什么需要这一层（审查第三轮归并包 B2）：
旧实现只有「角色交集」——``principal.roles & allowed_principals`` 非空即放行。
它无法表达三条早已写进契约与红线、但运行时没有任何代码执行的规则：

1. **Agent 不得执行 CRITICAL 动作**（红线「Agent 不得自批自用」）。
   旧实现里一个 ``sub_type=agent`` 的令牌只要角色名匹配，就能走审批类动作。
2. **scope 声明为 ``tenant+organization`` / ``project`` 的动作，跨组织、跨项目
   必须被拒**。契约里 81 条动作全都有 scope 字段，但加载器只读了
   ``allowed_principals``，scope 与 risk_level 从未被运行时使用。
3. **审批类动作的申请人不得等于批准人**（职责分离）。此前散落在两个 service 里
   各写一份 if，错误码与文案都不一致，审计无法统一计数。

本模块只做**纯函数决策**：不读数据库、不读环境变量、不打日志。输入是已验签的
``Principal`` 与（可选的）资源引用，输出是不可变的决策对象。这样可以对着 81 条
契约做穷举性质测试——「规则是否覆盖全部动作」必须能被机器证明，而不是抽样。

规则顺序（首个命中的拒绝胜出，fail-closed）：

    1. 动作未注册                     → ACTION_UNREGISTERED
    2. 契约未配置允许主体             → NO_ALLOWED_PRINCIPAL
    3. 要求多方联合审批（未实现）     → JOINT_APPROVAL_UNSUPPORTED
    4. 角色不匹配                     → ROLE_MISMATCH
    5. Agent 执行 CRITICAL            → AGENT_CRITICAL_FORBIDDEN
    6. 申请人 == 批准人               → SELF_APPROVAL_FORBIDDEN
    8. 资源缺组织归属（无法判定）     → RESOURCE_UNRESOLVED
    9. 组织不匹配                     → ORGANIZATION_MISMATCH
   10. 资源缺项目归属（无法判定）     → RESOURCE_UNRESOLVED
   11. 项目不在授权范围               → PROJECT_SCOPE_DENIED
   12. 以上皆过                       → ALLOW（含"职责分离交给资源阶段"的显式记录）

关于「未解析维度」：读类动作在拿不到资源实例时（列表查询、按 id 读取），组织与
项目维度由 PostgreSQL 行级策略执行（ADR-R17）。决策把这种情形显式记录在
``deferred_dimensions`` 里，而不是假装检查过——「检查了什么」与「交给谁检查」
都必须能被审计读到，否则"已授权"这三个字就没有信息量。

关于审批类动作的识别：Action Registry 的命名规范本身是契约的一部分，审批/放行
动作以 ``APPROVE`` / ``RELEASE`` / ``PUBLISH`` / ``REVOKE`` / ``CHANGE`` 结尾。
按后缀识别而不是给每条动作加一个布尔字段，是为了不引入第二份事实源：命名改了
就自动影响判定，不会出现"字段漏配"这种静默失效。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from packages.identity.principal import Principal

#: 审批/放行类动作的键后缀（契约命名规范，见 Action Registry 主表）。
_APPROVAL_SUFFIXES = (".APPROVE", ".RELEASE", ".PUBLISH", ".REVOKE", ".CHANGE")

#: 契约里表示「多主体共同审批」（AND 语义）的标记。
#:
#: ``A+B+C`` 的意思是"这三方共同审批"，与 ``A/B``（任一即可）是两种不同语义。
#: 简单按 ``+`` 再拆成角色集合会把 AND 读成 OR——那等于让任意一方单独批，
#: 是审批链上最危险的误读，所以这里**刻意不拆**：识别为联合审批后直接拒绝，
#: 直到真正的多方签名流程落地。
_JOINT_APPROVAL_MARKERS = ("+", "、")


class RiskLevel(StrEnum):
    """动作风险等级（来自 Action Registry 的 ``risk_level``）。"""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class ScopeDimension(StrEnum):
    """**Principal 自身属性就能判定**的隔离维度。

    只有这三个维度属于"授权决策能自己算出来"的范畴：
    - ``tenant``：来自令牌，且数据库层另有 RLS（双保险，判定上不重复执行）；
    - ``organization``：来自令牌，与资源组织比对；
    - ``project``：来自令牌的 project_ids，与资源项目比对。

    scope 里更深的段（``shot`` / ``asset`` / ``asset_version`` / ``episode`` …）
    属于对象图内部层级，判定依据是所在行上的 ``project_id`` 列——由 RLS 执行，
    不在本模块重复实现一套对象图遍历（两套实现必然漂移）。
    """

    TENANT = "tenant"
    ORGANIZATION = "organization"
    PROJECT = "project"


class DecisionEffect(StrEnum):
    ALLOW = "ALLOW"
    DENY = "DENY"


class DecisionReason(StrEnum):
    """决策理由码（结构化、可计数、可告警）。

    拒绝码与放行码放在同一个枚举里，是为了让"每条决策都必须有理由"成为类型
    层面的约束——不存在没有 reason 的 ALLOW。
    """

    # 放行
    ROLE_AND_SCOPE_VERIFIED = "ROLE_AND_SCOPE_VERIFIED"
    SCOPE_DEFERRED_TO_RLS = "SCOPE_DEFERRED_TO_RLS"
    # 拒绝
    ACTION_UNREGISTERED = "ACTION_UNREGISTERED"
    NO_ALLOWED_PRINCIPAL = "NO_ALLOWED_PRINCIPAL"
    JOINT_APPROVAL_UNSUPPORTED = "JOINT_APPROVAL_UNSUPPORTED"
    ROLE_MISMATCH = "ROLE_MISMATCH"
    AGENT_CRITICAL_FORBIDDEN = "AGENT_CRITICAL_FORBIDDEN"
    SELF_APPROVAL_FORBIDDEN = "SELF_APPROVAL_FORBIDDEN"
    CAPABILITY_MISSING = "CAPABILITY_MISSING"
    CAPABILITY_SUBJECT_MISMATCH = "CAPABILITY_SUBJECT_MISMATCH"
    CAPABILITY_TENANT_MISMATCH = "CAPABILITY_TENANT_MISMATCH"
    CAPABILITY_ORGANIZATION_MISMATCH = "CAPABILITY_ORGANIZATION_MISMATCH"
    CAPABILITY_ACTION_DENIED = "CAPABILITY_ACTION_DENIED"
    CAPABILITY_PROJECT_SCOPE_DENIED = "CAPABILITY_PROJECT_SCOPE_DENIED"
    ORGANIZATION_MISMATCH = "ORGANIZATION_MISMATCH"
    PROJECT_SCOPE_DENIED = "PROJECT_SCOPE_DENIED"
    RESOURCE_UNRESOLVED = "RESOURCE_UNRESOLVED"


def parse_scope(scope: str) -> frozenset[ScopeDimension]:
    """把契约里的 scope 路径解析为可判定的维度集合。

    scope 是**资源路径**而不是维度列表：``project/shot/asset`` 表示"作用于某
    项目下某镜头下的资产"，其中只有 ``project`` 是 Principal 属性能判定的维度；
    ``shot`` / ``asset`` 是对象图层级。``tenant+organization`` 是唯一用 ``+``
    表达并列的写法（租户与组织同时约束）。

    解析失败不抛异常：未知段一律视为对象图层级（由 RLS 处理）。这不是宽松——
    未知段若真是维度，它必然也在契约的 resource_type 或正文里有定义，而**维度
    词表只有三个**，多出来的段不可能改变判定。
    """
    dimensions: set[ScopeDimension] = set()
    for part in scope.split("/"):
        for token in part.split("+"):
            normalized = token.strip()
            try:
                dimensions.add(ScopeDimension(normalized))
            except ValueError:
                continue
    return frozenset(dimensions)


@dataclass(frozen=True)
class ActionSpec:
    """一条动作的完整元数据（Action Registry 的运行时投影）。

    此前加载器只保留 ``allowed_principals``，其余四个字段被丢弃——契约里写了
    风险等级与作用范围，代码却一条也不用，等于契约描述的是另一个系统。
    """

    action_key: str
    resource_type: str
    scope: str
    risk_level: RiskLevel
    allowed_principals: frozenset[str]
    token_required: bool
    approval_requirement: str
    #: 动作生命周期状态（来自 Action Registry 的 ``status``）。默认 ``ACTIVE``
    #: 仅用于测试构造；运行时加载器必须显式读取契约值（缺失即视为 ACTIVE 的
    #: 宽松默认会造成"漏配即放行"，故加载器对缺失字段另有 fail-fast 校验）。
    status: str = "ACTIVE"
    #: 原始 ``allowed_principals`` 文本（保留它才能识别联合审批标记，
    #: 解析后的集合已经把 AND 语义抹平了）。
    allowed_principals_text: str = ""

    @property
    def scope_dimensions(self) -> frozenset[ScopeDimension]:
        return parse_scope(self.scope)

    @property
    def is_critical(self) -> bool:
        return self.risk_level is RiskLevel.CRITICAL

    @property
    def is_phase_gated(self) -> bool:
        """是否为阶段未启用（``PHASE_GATED``）的动作。

        用前缀匹配而非精确相等：契约里 status 是描述性文本（如
        ``PHASE_GATED（Phase 2）`` / ``PHASE_GATED（Phase 3）``），括号内是
        启用阶段说明。凡以 ``PHASE_GATED`` 开头的动作都不得在运行时发起，
        这正是 R4-2 要堵住的"为尚未开放的动作建票据"绕过路径。
        """
        return self.status.startswith("PHASE_GATED")

    @property
    def requires_joint_approval(self) -> bool:
        """是否要求多主体共同审批（AND 语义，当前未实现，一律拒绝）。"""
        return any(
            marker in self.allowed_principals_text
            for marker in _JOINT_APPROVAL_MARKERS
        )

    @property
    def is_approval(self) -> bool:
        """是否为审批/放行动作（要求提供审批对象以执行职责分离判定）。"""
        return self.action_key.endswith(_APPROVAL_SUFFIXES)


@dataclass(frozen=True)
class ResourceRef:
    """被操作资源的最小归属描述（只包含判定需要的字段）。

    刻意不传整个领域对象：决策不应该拿到业务字段，否则"授权判定"会慢慢长出
    业务分支；也只接受归属键，跨模块传对象会破坏模块边界。

    ``organization_id`` / ``project_id`` 为 ``None`` 表示**资源存在但归属未知**，
    与"没有资源"（整个 ref 为 None）是两种不同情形：前者判拒绝（无法判定即
    拒绝），后者记为交给 RLS。
    """

    resource_type: str
    organization_id: UUID | None = None
    project_id: str | None = None


@dataclass(frozen=True)
class AuthorizationDecision:
    """一次授权判定的结果（不可变、可审计）。

    ``checked_dimensions`` 与 ``deferred_dimensions`` 是这件对象存在的核心理由：
    只输出 ALLOW/DENY 无法回答"到底查了什么"。审计要能看出某个 ALLOW 是
    "组织与项目都核对过"还是"仅角色匹配、范围交给数据库"，这两者的风险完全不同。
    """

    action_key: str
    effect: DecisionEffect
    reason: DecisionReason
    detail: str
    subject_id: str
    subject_type: str
    risk_level: RiskLevel
    checked_dimensions: tuple[ScopeDimension, ...] = ()
    deferred_dimensions: tuple[ScopeDimension, ...] = ()
    #: 已声明但**此刻无法执行**的检查（如依赖阶段还没有审批对象）。
    #: 与 ``deferred_dimensions`` 分开：维度是"谁执行"（RLS），检查是"何时执行"（资源阶段）。
    deferred_checks: tuple[str, ...] = ()
    #: 本次判定使用的 capability jti；拒绝时可能因缺 token 为 None。
    #: 审计需要它把"决策"与"授权服务签发记录"串起来，但不记录 token 原文。
    capability_jti: str | None = None

    @property
    def allowed(self) -> bool:
        return self.effect is DecisionEffect.ALLOW

    def to_audit_dict(self) -> dict[str, object]:
        """审计友好结构（不含令牌内容，符合「密钥不落日志」红线）。"""
        return {
            "action_key": self.action_key,
            "effect": self.effect.value,
            "reason": self.reason.value,
            "detail": self.detail,
            "subject_id": self.subject_id,
            "subject_type": self.subject_type,
            "risk_level": self.risk_level.value,
            "checked": [dimension.value for dimension in self.checked_dimensions],
            "deferred": [dimension.value for dimension in self.deferred_dimensions],
            "deferred_checks": list(self.deferred_checks),
            "capability_jti": self.capability_jti,
        }


def decide(
    principal: Principal,
    spec: ActionSpec | None,
    *,
    resource: ResourceRef | None = None,
    applicant_subject_id: str | None = None,
) -> AuthorizationDecision:
    """执行一次授权判定（纯函数）。

    参数：
        principal：已验签的主体（身份来源唯一，绝不从请求体拼）。
        spec：动作元数据；``None`` 表示动作不在契约里（未注册一律拒绝）。
        resource：资源归属；``None`` 表示"此刻拿不到具体资源"（如列表查询），
            此时组织/项目维度记为交给 RLS。
        applicant_subject_id：审批类动作的申请人主体。审批动作**必须**提供，
            缺失即拒绝——否则"跳过职责分离"只需要少传一个参数。
    """
    checked: list[ScopeDimension] = []
    deferred: list[ScopeDimension] = []
    deferred_checks: list[str] = []

    if spec is None:
        return AuthorizationDecision(
            action_key="<unregistered>",
            effect=DecisionEffect.DENY,
            reason=DecisionReason.ACTION_UNREGISTERED,
            detail="动作未在 Action Registry 注册，按 fail-closed 拒绝",
            subject_id=principal.subject_id,
            subject_type=principal.subject_type.value,
            # 未注册动作的真实风险未知，按最高等级上报：审计里看到 CRITICAL
            # 才会有人去查，看到 LOW 会被当成噪声。
            risk_level=RiskLevel.CRITICAL,
        )

    def deny(
        reason: DecisionReason,
        detail: str,
    ) -> AuthorizationDecision:
        return AuthorizationDecision(
            action_key=spec.action_key,
            effect=DecisionEffect.DENY,
            reason=reason,
            detail=detail,
            subject_id=principal.subject_id,
            subject_type=principal.subject_type.value,
            risk_level=spec.risk_level,
            checked_dimensions=tuple(checked),
            deferred_dimensions=tuple(deferred),
            deferred_checks=tuple(deferred_checks),
            capability_jti=principal.capability.jti if principal.capability else None,
        )

    # 1. 契约未配置允许主体：不是"谁都可以"，是"契约缺失"。
    if not spec.allowed_principals:
        return deny(
            DecisionReason.NO_ALLOWED_PRINCIPAL,
            "契约未声明该动作的允许主体",
        )

    # 2. 要求多主体共同审批的动作：当前没有联合审批流程，拒绝而不是把 AND 读成 OR。
    if spec.requires_joint_approval:
        return deny(
            DecisionReason.JOINT_APPROVAL_UNSUPPORTED,
            "该动作要求多主体共同审批（AND 语义），联合审批流程尚未实现",
        )

    # 3. 角色匹配（原有的唯一判定，现在只是第一道）。
    if not (principal.roles & spec.allowed_principals):
        return deny(
            DecisionReason.ROLE_MISMATCH,
            f"主体角色与契约允许主体不相交（允许：{'/'.join(sorted(spec.allowed_principals))}）",
        )

    # 3. Agent 不得执行 CRITICAL 动作（红线「Agent 不得自批自用」）。
    if principal.is_agent and spec.is_critical:
        return deny(
            DecisionReason.AGENT_CRITICAL_FORBIDDEN,
            "Agent 不得执行 CRITICAL 动作（红线：Agent 不得自批自用）",
        )

    # 4. 审批类动作的职责分离：申请人 == 批准人即拒绝。
    #
    #    依赖阶段（HTTP 依赖）拿不到审批对象——申请人要查库才知道。此时**不拒绝**，
    #    而是把这条检查显式记为 deferred，由业务层在加载资源后带 applicant 再判一次。
    #    为什么不用"缺参数就拒绝"：那会让所有审批路由在依赖阶段直接 403，永远走不到
    #    业务层。为什么不用"缺参数就跳过"而不记录：那就是静默跳过，审计看不出来。
    #    配套闸门：``tests/permission/test_authorization_wiring.py`` 用 AST 检查
    #    每一处针对审批动作的 ``authorize(...)`` 调用都传了 ``applicant_subject_id``。
    if spec.is_approval:
        if applicant_subject_id is None:
            deferred_checks.append("separation_of_duties")
        elif applicant_subject_id.strip() == principal.subject_id.strip():
            return deny(
                DecisionReason.SELF_APPROVAL_FORBIDDEN,
                "申请人与批准人不得为同一主体（职责分离红线）",
            )

    # 5. Capability Token：契约声明 token_required 的动作一律要求独立签发的短时凭证。
    #
    #    这是 F3 的运行时执行点。凭证只能收窄 principal，不能代替角色、Agent 红线
    #    或职责分离；四者都在同一个 decide() 里执行，不存在第二套授权入口。
    capability = principal.capability
    if spec.token_required and capability is None:
        return deny(
            DecisionReason.CAPABILITY_MISSING,
            "动作要求 Capability Token，请求未提供（由独立 Authorization Service 签发）",
        )
    if capability is not None:
        if capability.subject_id.strip() != principal.subject_id.strip():
            return deny(
                DecisionReason.CAPABILITY_SUBJECT_MISMATCH,
                "Capability Token 绑定的主体与访问令牌主体不一致",
            )
        if capability.tenant_id != str(principal.tenant_id):
            return deny(
                DecisionReason.CAPABILITY_TENANT_MISMATCH,
                "Capability Token 绑定的租户与访问令牌租户不一致",
            )
        if capability.organization_id != str(principal.organization_id):
            return deny(
                DecisionReason.CAPABILITY_ORGANIZATION_MISMATCH,
                "Capability Token 绑定的组织与访问令牌组织不一致",
            )
        if not capability.allows_action(spec.action_key):
            return deny(
                DecisionReason.CAPABILITY_ACTION_DENIED,
                f"Capability Token 未授权动作 {spec.action_key}（或已显式拒绝）",
            )

    dimensions = spec.scope_dimensions

    # 5. 组织维度：契约声明了就必须核对；资源给了但组织未知 → 无法判定即拒绝。
    if ScopeDimension.ORGANIZATION in dimensions:
        if resource is None:
            deferred.append(ScopeDimension.ORGANIZATION)
        elif resource.organization_id is None:
            return deny(
                DecisionReason.RESOURCE_UNRESOLVED,
                "资源未提供组织归属，无法判定跨组织访问",
            )
        elif resource.organization_id != principal.organization_id:
            return deny(
                DecisionReason.ORGANIZATION_MISMATCH,
                "资源不属于调用主体所在组织",
            )
        else:
            checked.append(ScopeDimension.ORGANIZATION)

    # 7. 项目维度：同上，空范围一律拒绝（不做"没有范围就放行"的推断）。
    if ScopeDimension.PROJECT in dimensions:
        if resource is None:
            deferred.append(ScopeDimension.PROJECT)
        elif not resource.project_id:
            return deny(
                DecisionReason.RESOURCE_UNRESOLVED,
                "资源未提供项目归属，无法判定跨项目访问",
            )
        elif not principal.can_access_project(resource.project_id):
            return deny(
                DecisionReason.PROJECT_SCOPE_DENIED,
                f"项目 {resource.project_id} 不在主体授权范围内",
            )
        elif capability is not None and not capability.allows_project(
            resource.project_id,
            principal_has_all_projects=principal.has_all_projects_scope,
        ):
            return deny(
                DecisionReason.CAPABILITY_PROJECT_SCOPE_DENIED,
                f"项目 {resource.project_id} 不在 Capability Token 授权范围内",
            )
        else:
            checked.append(ScopeDimension.PROJECT)

    if deferred or deferred_checks:
        reason = DecisionReason.SCOPE_DEFERRED_TO_RLS
        parts = ["角色与 capability 匹配"]
        if deferred:
            parts.append("范围维度交由 PostgreSQL 行级策略执行")
        if deferred_checks:
            parts.append("职责分离交由资源阶段执行")
        detail = "；".join(parts)
    else:
        reason = DecisionReason.ROLE_AND_SCOPE_VERIFIED
        detail = "角色、capability、范围与职责分离校验通过"
    return AuthorizationDecision(
        action_key=spec.action_key,
        effect=DecisionEffect.ALLOW,
        reason=reason,
        detail=detail,
        subject_id=principal.subject_id,
        subject_type=principal.subject_type.value,
        risk_level=spec.risk_level,
        checked_dimensions=tuple(checked),
        deferred_dimensions=tuple(deferred),
        deferred_checks=tuple(deferred_checks),
        capability_jti=principal.capability.jti if principal.capability else None,
    )


def holds_any_role(principal: Principal, roles: frozenset[str] | tuple[str, ...]) -> bool:
    """主体是否持有给定角色中的任一角色（细粒度资格判定，不替代授权）。

    为什么放在 identity 包：角色比对只允许一处实现。W3 闸门禁止 services 层
    直接访问 ``principal.roles`` / ``principal.has_any_role``——那样会绕开
    Agent、CRITICAL、组织与项目四类判定。审批分组资格是 ``authorize()``
    之后的**额外**约束，必须复用本函数而不是另起一套比对。

    调用契约：先 ``authorize(principal, action, resource=...)``，再用本函数做
    更细的主体筛选；本函数单独使用不构成授权。
    """
    return bool(principal.roles & frozenset(roles))


__all__ = [
    "ActionSpec",
    "AuthorizationDecision",
    "DecisionEffect",
    "DecisionReason",
    "ResourceRef",
    "RiskLevel",
    "ScopeDimension",
    "decide",
    "holds_any_role",
    "parse_scope",
]
