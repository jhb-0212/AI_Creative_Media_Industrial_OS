"""职责分离（Separation of Duties）统一判定。

设计要点（F02 根治核心，消除重复机制）：
- 此前「申请人≠批准人」的职责分离红线被散落在 script_service 与
  asset_service 两处，各自用 ``requested_by == approved_by`` /
  ``created_by == approved_by`` 字符串比对，判定口径与错误语义不统一。
- 本模块收敛为单一判定函数 ``require_different_subject``，让「职责分离是
  两个不同真实主体」这一不变量只有一个权威实现，业务服务只调用不重写。

安全语义：
- 两个主体标识都来自可信 Principal（JWT sub claim），不是客户端请求体
  字符串——这是职责分离能被信任的前提（已在 P0-3 收口）。
- 判定严格区分「主体缺失」与「主体相同」两种失败：
  - 缺失（actor 为空）：审批链不完整，禁止放行（fail-closed）。
  - 相同：自批/自锁，直接拒绝。
  两者都抛 ``STATE_TRANSITION_DENIED``（与既有领域层约定一致：职责分离是
  状态转移的前置校验，而非依赖层授权失败），但 detail 区分，便于审计定位。
"""

from __future__ import annotations

from packages.errors.error_codes import DomainError, ErrorRegistry


def require_different_subject(
    *,
    actor: str,
    counterpart: str | None,
    action_label: str,
    actor_label: str = "当前主体",
    counterpart_label: str = "另一方主体",
) -> None:
    """断言两个职责主体必须不同（职责分离红线，fail-closed）。

    参数：
    - actor：当前执行主体（如批准人 approved_by），必须非空。
    - counterpart：对立职责主体（如申请人 requested_by / 创建人 created_by）。
      为 None 表示审批链缺失（无法比对），同样拒绝。
    - action_label：动作描述（如 ``SCRIPT.LOCK.APPROVE``），用于错误语义。
    - actor_label / counterpart_label：两个主体在错误信息中的可读称谓。

    失败语义：
    - counterpart 为空 → 审批链不完整，禁止批准（不可静默绕过比对）。
    - actor 为空 → 当前主体缺失，禁止（防御性，正常流程不会走到）。
    - actor == counterpart → 自批/自锁，拒绝。

    成功时无返回值；业务服务在状态推进前调用本函数完成红线校验。
    """
    actor = actor.strip()
    if not actor:
        raise DomainError(
            ErrorRegistry.STATE_TRANSITION_DENIED,
            detail=f"{action_label}：{actor_label}不能为空（职责分离不可绕过）",
        )
    if counterpart is None or not counterpart.strip():
        raise DomainError(
            ErrorRegistry.STATE_TRANSITION_DENIED,
            detail=(f"{action_label}：缺少{counterpart_label}，禁止批准（职责分离不可绕过）"),
        )
    if actor == counterpart.strip():
        raise DomainError(
            ErrorRegistry.STATE_TRANSITION_DENIED,
            detail=(
                f"{action_label}：{actor_label}与{counterpart_label}不得为同一主体（职责分离红线）"
            ),
        )
