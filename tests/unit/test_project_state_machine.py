"""Project 状态机单元测试（对齐 state_machines.yaml 契约）。

验证：
1. 所有已登记转移均可执行；
2. 所有未登记转移一律拒绝（31.5）；
3. 终态 ARCHIVED 不可原地复活（红线）。
"""

from __future__ import annotations

import pytest

from services.project_planning.domain.enums import ProjectStatus
from services.project_planning.domain.state_machine import (
    ALLOWED_TRANSITIONS,
    TERMINAL_STATES,
    assert_transition,
    can_transition,
)

# 契约权威转移表（与 state_machines.yaml Project 条目一致）
EXPECTED_TRANSITIONS: dict[ProjectStatus, set[ProjectStatus]] = {
    ProjectStatus.DRAFT: {ProjectStatus.ACTIVE, ProjectStatus.ARCHIVED},
    ProjectStatus.ACTIVE: {ProjectStatus.ON_HOLD, ProjectStatus.COMPLETED},
    ProjectStatus.ON_HOLD: {ProjectStatus.ACTIVE, ProjectStatus.ARCHIVED},
    ProjectStatus.COMPLETED: {ProjectStatus.ARCHIVED},
    ProjectStatus.ARCHIVED: set(),
}

ALL_STATES = list(ProjectStatus)


def test_transition_table_matches_contract() -> None:
    """转移表与冻结契约严格一致。"""
    assert set(ALLOWED_TRANSITIONS.keys()) == set(EXPECTED_TRANSITIONS.keys())
    for src, targets in EXPECTED_TRANSITIONS.items():
        assert set(ALLOWED_TRANSITIONS[src]) == targets


def test_all_registered_transitions_allowed() -> None:
    """所有已登记转移均合法。"""
    for src, targets in EXPECTED_TRANSITIONS.items():
        for tgt in targets:
            assert can_transition(src, tgt), f"{src.value} -> {tgt.value} 应合法"


def test_all_unregistered_transitions_denied() -> None:
    """所有未登记转移一律拒绝。"""
    for src in ALL_STATES:
        for tgt in ALL_STATES:
            if tgt in EXPECTED_TRANSITIONS[src]:
                continue
            assert not can_transition(src, tgt), (
                f"{src.value} -> {tgt.value} 应被拒绝"
            )


def test_archived_is_terminal_and_cannot_revive() -> None:
    """ARCHIVED 为终态，不可原地复活。"""
    assert ProjectStatus.ARCHIVED in TERMINAL_STATES
    assert ALLOWED_TRANSITIONS[ProjectStatus.ARCHIVED] == frozenset()


def test_assert_transition_raises_on_illegal() -> None:
    """assert_transition 对未登记转移抛 ValueError。"""
    with pytest.raises(ValueError):
        assert_transition(ProjectStatus.ARCHIVED, ProjectStatus.ACTIVE)
    with pytest.raises(ValueError):
        assert_transition(ProjectStatus.COMPLETED, ProjectStatus.ACTIVE)


def test_assert_transition_passes_on_legal() -> None:
    """assert_transition 对已登记转移不抛异常。"""
    assert_transition(ProjectStatus.DRAFT, ProjectStatus.ACTIVE)
    assert_transition(ProjectStatus.ACTIVE, ProjectStatus.COMPLETED)
