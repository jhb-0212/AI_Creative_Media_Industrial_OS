"""Project 状态机（对齐 contracts/state_machines/state_machines.yaml）。

设计要点（不屎山代码）：
- 状态机为纯声明式数据 + 校验函数，无副作用、无框架依赖。
- 转移表单一权威来源，与冻结契约 state_machines.yaml 中 Project 条目严格对齐，
  未登记转移一律拒绝（31.5 规则）。
- 终态（ARCHIVED）不可原地复活（红线「终态不可原地复活」）。

契约权威定义（state_machines.yaml → objects → Project）：
    DRAFT→ACTIVE→ON_HOLD；ACTIVE→COMPLETED；ON_HOLD→ACTIVE；DRAFT/ON_HOLD/COMPLETED→ARCHIVED
"""

from __future__ import annotations

from services.project_planning.domain.enums import ProjectStatus

# 单一权威转移表：source -> 允许的 target 集合。
# 与契约 state_machines.yaml 的 Project.states 严格一致。
ALLOWED_TRANSITIONS: dict[ProjectStatus, frozenset[ProjectStatus]] = {
    ProjectStatus.DRAFT: frozenset({ProjectStatus.ACTIVE, ProjectStatus.ARCHIVED}),
    ProjectStatus.ACTIVE: frozenset({ProjectStatus.ON_HOLD, ProjectStatus.COMPLETED}),
    ProjectStatus.ON_HOLD: frozenset({ProjectStatus.ACTIVE, ProjectStatus.ARCHIVED}),
    ProjectStatus.COMPLETED: frozenset({ProjectStatus.ARCHIVED}),
    # 终态：不可原地复活（红线）
    ProjectStatus.ARCHIVED: frozenset(),
}

# 终态集合（不可再迁移）
TERMINAL_STATES: frozenset[ProjectStatus] = frozenset({ProjectStatus.ARCHIVED})


def can_transition(source: ProjectStatus, target: ProjectStatus) -> bool:
    """判断 source -> target 是否为已登记转移。"""
    return target in ALLOWED_TRANSITIONS.get(source, frozenset())


def assert_transition(source: ProjectStatus, target: ProjectStatus) -> None:
    """校验转移合法性，未登记则抛出 ValueError（31.5 未登记转移一律拒绝）。"""
    if not can_transition(source, target):
        raise ValueError(
            f"非法状态转移：{source.value} -> {target.value}（未登记，已拒绝）"
        )
