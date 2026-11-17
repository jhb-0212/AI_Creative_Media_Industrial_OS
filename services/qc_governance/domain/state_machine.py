"""质量治理域状态机（31.5：未登记转移一律拒绝）。

QCReviewSession：OPEN→IN_REVIEW→DECISION_PENDING→RESOLVED→GATE_ELIGIBLE→CLOSED。

- 契约登记：``contracts/state_machines/state_machines.yaml``（C.7 权威状态链，
  C.3 的 ``RESOLVED→GATE_ELIGIBLE/CLOSED`` 按 31.5 裁决 ④ 不产生
  ``RESOLVED→CLOSED`` 边）；
- 数据库层镜像：迁移 ``f1a2b3c4d5e6`` 播种同名转移表 + 守卫触发器（ADR-R20），
  直连 SQL 的写入不经服务层，必须在数据库里也拦一次；
- 一致性由 ``tests/unit/test_qc_review_parity.py`` 守（迁移字面量 ↔ 枚举 ↔ 契约）。

为什么 ``WAIVE_WITH_AUTHORITY`` 没有任何边：见 ``enums.QCReviewSessionStatus``
的说明——它是人工结论类型，被 enum_registry 误登记为状态值。按「不猜契约」
保留取值、不造来源边；该状态在 ``state_machine_gaps.yaml`` 不需要登记，因为
可达性校验取契约 ``states`` 文本（C.7 链），文本里没有这个状态。
"""

from __future__ import annotations

from packages.errors.error_codes import DomainError, ErrorRegistry
from services.qc_governance.domain.enums import QCReviewSessionStatus

QC_REVIEW_SESSION_ALLOWED_TRANSITIONS: dict[
    QCReviewSessionStatus, frozenset[QCReviewSessionStatus]
] = {
    QCReviewSessionStatus.OPEN: frozenset({QCReviewSessionStatus.IN_REVIEW}),
    QCReviewSessionStatus.IN_REVIEW: frozenset(
        {QCReviewSessionStatus.DECISION_PENDING}
    ),
    QCReviewSessionStatus.DECISION_PENDING: frozenset(
        {QCReviewSessionStatus.RESOLVED}
    ),
    # RESOLVED 只有一条出边：必须经 GATE_ELIGIBLE（31.5 裁决 ④ 禁止跳关）。
    QCReviewSessionStatus.RESOLVED: frozenset({QCReviewSessionStatus.GATE_ELIGIBLE}),
    QCReviewSessionStatus.GATE_ELIGIBLE: frozenset({QCReviewSessionStatus.CLOSED}),
    # CLOSED 无出边：会话关闭是终态，事实不可原地复活（红线）。
    QCReviewSessionStatus.CLOSED: frozenset(),
    # 可表示、不可进入：见模块 docstring。
    QCReviewSessionStatus.WAIVE_WITH_AUTHORITY: frozenset(),
}


def can_transition_review_session(
    source: QCReviewSessionStatus, target: QCReviewSessionStatus
) -> bool:
    """判断会话的 source -> target 是否为已登记转移。"""
    return target in QC_REVIEW_SESSION_ALLOWED_TRANSITIONS.get(source, frozenset())


def allowed_targets(
    source: QCReviewSessionStatus,
) -> frozenset[QCReviewSessionStatus]:
    """列出当前状态的全部合法下一步（拒绝时回给调用方，便于定位）。"""
    return QC_REVIEW_SESSION_ALLOWED_TRANSITIONS.get(source, frozenset())


def assert_transition_review_session(
    source: QCReviewSessionStatus, target: QCReviewSessionStatus
) -> None:
    """校验会话转移合法性，未登记则抛结构化错误（带 from/to/允许的下一步）。

    ``details`` 里给 ``allowed_targets`` 而不是只说"非法"：调用方最常犯的错
    就是跳关（RESOLVED 直接要求 CLOSED），告诉他"你现在只能去 GATE_ELIGIBLE"
    比一句 state_transition_denied 有用得多，也让审计能区分"绕过"与"手滑"。
    """
    if can_transition_review_session(source, target):
        return
    raise DomainError(
        ErrorRegistry.STATE_TRANSITION_DENIED,
        detail=f"非法状态转移：{source.value} -> {target.value}（未登记，已拒绝）",
        context={
            "from_status": source.value,
            "to_status": target.value,
            "allowed_targets": sorted(t.value for t in allowed_targets(source)),
        },
    )


__all__ = [
    "QC_REVIEW_SESSION_ALLOWED_TRANSITIONS",
    "allowed_targets",
    "assert_transition_review_session",
    "can_transition_review_session",
]
