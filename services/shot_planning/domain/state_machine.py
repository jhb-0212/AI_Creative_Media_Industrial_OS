"""Shot-planning 领域状态机（单一事实源）。

设计要点（对齐 project_planning 状态机范式）：
- 纯声明式数据 + 校验函数，无副作用、无框架依赖。
- 转移表集中在**本模块**，便于 contract_check 统一反射比对。
- 终态不可原地复活（红线「终态不可原地复活」）。

契约权威定义：
    ShotDraft:     DRAFT→CONFIRMED/SUPERSEDED；CONFIRMED→SUPERSEDED
    ShotRevision:  append-only 事实，无状态转移——版本递增不覆盖（8.2），
                   最新版本由 revision 号派生，不主动标记旧修订。
    ConstraintPack: DRAFT→ACTIVE；ACTIVE→STALE/SUPERSEDED/REVOKED（7.9，
                   已在 scripts/contract_check.py 登记为机读比对源）。
    ShotVisualPlan: DRAFT→REVIEW→APPROVED；可 PARTIALLY_LOCKED/LOCKED；
                   拒绝为 REJECTED（C.7）。锁定/拒绝的补充边无法从契约
                   states 文本机读解析（文本只给链式主路径），故该对象
                   不进 contract_check 的机读比对，一致性由
                   tests/unit/test_enum_parity.py 对迁移字面量双向守住。
"""

from __future__ import annotations

from packages.errors.error_codes import DomainError, ErrorRegistry
from services.shot_planning.domain.enums import (
    ConstraintPackStatus,
    ShotDraftStatus,
    ShotVisualPlanStatus,
)

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


# --------------------------------------------------------------------------- #
# ConstraintPack 状态机（7.9，契约机读比对源）
# --------------------------------------------------------------------------- #
CONSTRAINT_PACK_ALLOWED_TRANSITIONS: dict[
    ConstraintPackStatus, frozenset[ConstraintPackStatus]
] = {
    ConstraintPackStatus.DRAFT: frozenset({ConstraintPackStatus.ACTIVE}),
    ConstraintPackStatus.ACTIVE: frozenset(
        {
            ConstraintPackStatus.STALE,
            ConstraintPackStatus.SUPERSEDED,
            ConstraintPackStatus.REVOKED,
        }
    ),
    # STALE / SUPERSEDED / REVOKED 均无出边：STALE 须重新确认（重建包走
    # 新版本），SUPERSEDED / REVOKED 是终态，不可原地复活（红线）。
    ConstraintPackStatus.STALE: frozenset(),
    ConstraintPackStatus.SUPERSEDED: frozenset(),
    ConstraintPackStatus.REVOKED: frozenset(),
}


def can_transition_constraint_pack(
    source: ConstraintPackStatus, target: ConstraintPackStatus
) -> bool:
    """判断 ConstraintPack 的 source -> target 是否为已登记转移。"""
    return target in CONSTRAINT_PACK_ALLOWED_TRANSITIONS.get(source, frozenset())


def assert_transition_constraint_pack(
    source: ConstraintPackStatus, target: ConstraintPackStatus
) -> None:
    """校验 ConstraintPack 转移合法性，未登记则抛 DomainError。"""
    if not can_transition_constraint_pack(source, target):
        raise DomainError(
            ErrorRegistry.STATE_TRANSITION_DENIED,
            detail=f"非法状态转移：{source.value} -> {target.value}（未登记，已拒绝）",
        )


# --------------------------------------------------------------------------- #
# ShotVisualPlan 状态机（C.7；见模块 docstring 的机读口径说明）
# --------------------------------------------------------------------------- #
SHOT_VISUAL_PLAN_ALLOWED_TRANSITIONS: dict[
    ShotVisualPlanStatus, frozenset[ShotVisualPlanStatus]
] = {
    ShotVisualPlanStatus.DRAFT: frozenset({ShotVisualPlanStatus.REVIEW}),
    ShotVisualPlanStatus.REVIEW: frozenset(
        {ShotVisualPlanStatus.APPROVED, ShotVisualPlanStatus.REJECTED}
    ),
    ShotVisualPlanStatus.APPROVED: frozenset(
        {
            ShotVisualPlanStatus.PARTIALLY_LOCKED,
            ShotVisualPlanStatus.LOCKED,
        }
    ),
    ShotVisualPlanStatus.PARTIALLY_LOCKED: frozenset(
        {ShotVisualPlanStatus.LOCKED}
    ),
    # REJECTED / LOCKED 是终态：变更须新建 revision（C.7 版本不可变规则），
    # 终态不可原地复活（红线）。
    ShotVisualPlanStatus.REJECTED: frozenset(),
    ShotVisualPlanStatus.LOCKED: frozenset(),
}


def can_transition_visual_plan(
    source: ShotVisualPlanStatus, target: ShotVisualPlanStatus
) -> bool:
    """判断 ShotVisualPlan 的 source -> target 是否为已登记转移。"""
    return target in SHOT_VISUAL_PLAN_ALLOWED_TRANSITIONS.get(source, frozenset())


def assert_transition_visual_plan(
    source: ShotVisualPlanStatus, target: ShotVisualPlanStatus
) -> None:
    """校验 ShotVisualPlan 转移合法性，未登记则抛 DomainError。"""
    if not can_transition_visual_plan(source, target):
        raise DomainError(
            ErrorRegistry.STATE_TRANSITION_DENIED,
            detail=f"非法状态转移：{source.value} -> {target.value}（未登记，已拒绝）",
        )
