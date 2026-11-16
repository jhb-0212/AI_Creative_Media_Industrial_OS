"""授权强制执行点（决策 + 契约加载 + 审计留痕 + 拒绝转结构化错误）。

三层职责，依赖单向（``authorization`` ← ``action_registry`` ← ``enforcement``）：

1. ``packages.identity.authorization``：纯函数决策（无 IO，可对 81 条契约穷举测试）。
2. ``packages.identity.action_registry``：从契约加载 ``ActionSpec``。
3. **本模块**：把三者串起来——取 spec、决策、记审计、拒绝时抛 ``DomainError``。

为什么不做成"只决策不抛错"：调用方如果拿到一个 ``allowed=False`` 的对象却忘了
检查，等于没有授权。**拒绝必须是异常**，让"忘记判断"在代码路径上直接触发 403，
而不是静默放行。

为什么放在 ``packages`` 而不是 ``services/identity_access``：业务服务（service 层）
在拿到资源实例后也要执行资源级判定（例如资产批准前要知道资产的归属与创建者），
而 ``services → services`` 的直接依赖被 R2 闸门禁止。授权是横切能力，归属 packages。

审计口径：拒绝按 INFO 记一条结构化日志（事后必可追溯）；放行按 DEBUG（量大，
且可由请求日志 + 策略版本重建）。日志只含主体标识与理由码，**不含令牌内容**
（红线「密钥不落日志」）。
"""

from __future__ import annotations

import logging

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.identity.action_registry import action_spec
from packages.identity.authorization import (
    AuthorizationDecision,
    ResourceRef,
    decide,
)
from packages.identity.principal import Principal

_logger = logging.getLogger("afs.authorization")


def _record(decision: AuthorizationDecision) -> None:
    """记录决策（拒绝必留痕，放行只留 debug）。"""
    payload = decision.to_audit_dict()
    if decision.allowed:
        _logger.debug("authorization allow %s", payload)
    else:
        _logger.info("authorization deny %s", payload)


def authorize(
    principal: Principal,
    action_key: str,
    *,
    resource: ResourceRef | None = None,
    applicant_subject_id: str | None = None,
) -> AuthorizationDecision:
    """执行授权判定；拒绝时抛 403（``AUTH_SCOPE_DENIED``），放行返回决策对象。

    返回值刻意给调用方：审计事件（Outbox payload）需要写入"依据哪个理由码放行"，
    事后才能回答"当时凭什么批的"。丢弃返回值不影响安全性，只丢掉可追溯性。

    参数：
        resource：资源归属。给出时会真正核对组织/项目维度；``None`` 表示此刻
            取不到资源（列表查询、按 id 读取），该维度由数据库行级策略执行，
            决策对象里的 ``deferred_dimensions`` 会记录这一点。
        applicant_subject_id：审批类动作的申请人主体，**必须提供**（缺失即拒绝）。
    """
    decision = decide(
        principal,
        action_spec(action_key),
        resource=resource,
        applicant_subject_id=applicant_subject_id,
    )
    _record(decision)
    if not decision.allowed:
        raise DomainError(
            ErrorRegistry.AUTH_SCOPE_DENIED,
            detail=f"{decision.reason.value}: {decision.detail}（动作 {action_key}）",
        )
    return decision


__all__ = ["authorize"]
