"""Project-planning 领域状态机（单一事实源，对齐 contracts/state_machines/state_machines.yaml）。

设计要点（不屎山代码）：
- 状态机为纯声明式数据 + 校验函数，无副作用、无框架依赖。
- 每个对象的转移表集中在**本模块**（而非散落于各聚合根内部），
  使 contract_check.py 能统一反射比对，避免「Project 用 state_machine.py、
  ScriptRevision 用内嵌转移表」的落点不一致。
- 转移表与冻结契约 state_machines.yaml 严格对齐，未登记转移一律拒绝（31.5）。
- 终态不可原地复活（红线「终态不可原地复活」）。

契约权威定义（state_machines.yaml → objects）：
    Project:        DRAFT→ACTIVE→ON_HOLD；ACTIVE→COMPLETED；ON_HOLD→ACTIVE；
                    DRAFT/ON_HOLD/COMPLETED→ARCHIVED
    ScriptRevision: DRAFT→LOCKED；DRAFT/LOCKED→SUPERSEDED
    Episode:        DRAFT→ACTIVE→ARCHIVED
"""

from __future__ import annotations

from services.project_planning.domain.enums import (
    EpisodeStatus,
    ProjectStatus,
    ScriptStatus,
)

# --------------------------------------------------------------------------- #
# 单一权威转移表：object -> { source: 允许的 target 集合 }
# 与契约 state_machines.yaml 各对象的状态链严格一致。
# --------------------------------------------------------------------------- #
ALLOWED_TRANSITIONS: dict[ProjectStatus, frozenset[ProjectStatus]] = {
    ProjectStatus.DRAFT: frozenset({ProjectStatus.ACTIVE, ProjectStatus.ARCHIVED}),
    ProjectStatus.ACTIVE: frozenset({ProjectStatus.ON_HOLD, ProjectStatus.COMPLETED}),
    ProjectStatus.ON_HOLD: frozenset({ProjectStatus.ACTIVE, ProjectStatus.ARCHIVED}),
    ProjectStatus.COMPLETED: frozenset({ProjectStatus.ARCHIVED}),
    # 终态：不可原地复活（红线）
    ProjectStatus.ARCHIVED: frozenset(),
}

# ScriptRevision 状态转移表（两步审批流：DRAFT→LOCK_PENDING→LOCKED）。
SCRIPT_ALLOWED_TRANSITIONS: dict[ScriptStatus, frozenset[ScriptStatus]] = {
    ScriptStatus.DRAFT: frozenset(
        {ScriptStatus.LOCK_PENDING, ScriptStatus.SUPERSEDED}
    ),
    ScriptStatus.LOCK_PENDING: frozenset(
        {ScriptStatus.LOCKED, ScriptStatus.SUPERSEDED}
    ),
    ScriptStatus.LOCKED: frozenset({ScriptStatus.SUPERSEDED}),
    ScriptStatus.SUPERSEDED: frozenset(),  # 终态不可原地复活
}

# Episode 状态转移表（Phase 1 主链新增建模）。
EPISODE_ALLOWED_TRANSITIONS: dict[EpisodeStatus, frozenset[EpisodeStatus]] = {
    EpisodeStatus.DRAFT: frozenset({EpisodeStatus.ACTIVE, EpisodeStatus.ARCHIVED}),
    EpisodeStatus.ACTIVE: frozenset({EpisodeStatus.ARCHIVED}),
    EpisodeStatus.ARCHIVED: frozenset(),  # 终态不可原地复活
}

# --------------------------------------------------------------------------- #
# 终态集合（不可再迁移）
# --------------------------------------------------------------------------- #
TERMINAL_STATES: frozenset[ProjectStatus] = frozenset({ProjectStatus.ARCHIVED})


def can_transition(source: ProjectStatus, target: ProjectStatus) -> bool:
    """判断 Project 的 source -> target 是否为已登记转移。"""
    return target in ALLOWED_TRANSITIONS.get(source, frozenset())


def assert_transition(source: ProjectStatus, target: ProjectStatus) -> None:
    """校验 Project 转移合法性，未登记则抛出 ValueError（31.5 未登记转移一律拒绝）。"""
    if not can_transition(source, target):
        raise ValueError(
            f"非法状态转移：{source.value} -> {target.value}（未登记，已拒绝）"
        )


def can_transition_script(source: ScriptStatus, target: ScriptStatus) -> bool:
    """判断 ScriptRevision 的 source -> target 是否为已登记转移。"""
    return target in SCRIPT_ALLOWED_TRANSITIONS.get(source, frozenset())


def assert_transition_script(source: ScriptStatus, target: ScriptStatus) -> None:
    """校验 ScriptRevision 转移合法性，未登记则抛出 ValueError。"""
    if not can_transition_script(source, target):
        raise ValueError(
            f"非法状态转移：{source.value} -> {target.value}（未登记，已拒绝）"
        )


def can_transition_episode(source: EpisodeStatus, target: EpisodeStatus) -> bool:
    """判断 Episode 的 source -> target 是否为已登记转移。"""
    return target in EPISODE_ALLOWED_TRANSITIONS.get(source, frozenset())


def assert_transition_episode(source: EpisodeStatus, target: EpisodeStatus) -> None:
    """校验 Episode 转移合法性，未登记则抛出 ValueError。"""
    if not can_transition_episode(source, target):
        raise ValueError(
            f"非法状态转移：{source.value} -> {target.value}（未登记，已拒绝）"
        )
