"""Shot-planning 领域状态机（单一事实源）。

设计要点（对齐 project_planning 状态机范式）：
- 纯声明式数据 + 校验函数，无副作用、无框架依赖。
- 转移表集中在**本模块**，便于 contract_check 统一反射比对。
- 终态不可原地复活（红线「终态不可原地复活」）。

契约权威定义：
    ShotDraft:     DRAFT→CONFIRMED/SUPERSEDED；CONFIRMED→SUPERSEDED
    ShotRevision:  append-only 事实，无状态转移——版本递增不覆盖（8.2），
                   最新版本由 revision 号派生，不主动标记旧修订。
"""

from __future__ import annotations

from packages.errors.error_codes import DomainError, ErrorRegistry
from services.shot_planning.domain.enums import ShotDraftStatus

# --------------------------------------------------------------------------- #
# 单一权威转移表：object -> { source: 允许的 target 集合 }
# --------------------------------------------------------------------------- #
DRAFT_ALLOWED_TRANSITIONS: dict[
    ShotDraftStatus, frozenset[ShotDraftStatus]
] = {
    ShotDraftStatus.DRAFT: frozenset(
        {ShotDraftStatus.CONFIRMED, ShotDraftStatus.SUPERSEDED}
    ),
    ShotDraftStatus.CONFIRMED: frozenset({ShotDraftStatus.SUPERSEDED}),
    # 终态不可原地复活（红线）
    ShotDraftStatus.SUPERSEDED: frozenset(),
}


def can_transition_draft(
    source: ShotDraftStatus, target: ShotDraftStatus
) -> bool:
    """判断 ShotDraft 的 source -> target 是否为已登记转移。"""
    return target in DRAFT_ALLOWED_TRANSITIONS.get(source, frozenset())


def assert_transition_draft(
    source: ShotDraftStatus, target: ShotDraftStatus
) -> None:
    """校验 ShotDraft 转移合法性，未登记则抛 DomainError。"""
    if not can_transition_draft(source, target):
        raise DomainError(
            ErrorRegistry.STATE_TRANSITION_DENIED,
            detail=f"非法状态转移：{source.value} -> {target.value}（未登记，已拒绝）",
        )
