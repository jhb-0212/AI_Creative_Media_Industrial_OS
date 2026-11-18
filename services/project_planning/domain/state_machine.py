"""项目域状态机（单一事实源，对齐 contracts/state_machines/state_machines.yaml）。

本轮结构性变化（N-07 根治）：**从邻接集改为边表，所有权真被执行**
------------------------------------------------------------
整改前，本模块的转移表是 ``{source: frozenset(target)}``——只回答「能不能走」，
不回答「够不够格走」。``owner_action`` 是服务层传给 ``advance_status`` 的一个
字符串，被写进迁移事实与事件，但**从不参与准入**。于是：

* ``SCRIPT.LOCKED``（归 ``SCRIPT.LOCK.APPROVE``）与 ``SCRIPT.SUPERSEDED``
  （归 ``SCRIPT.IMPORT``）在运行时是同一种通行证，区别只是一行自称；
* 审计表会忠实记下「谁走的」，但那个「谁」是调用方**填**的，不是系统**判**的。

现在每条边由共享层 ``packages/shared/lifecycle.py`` 的 ``Transition`` 声明，
带 ``owner_action``；聚合根 ``transition_to(target, audit_ref, action)`` 必须
接收 action 并在内部走 ``edge_owned_by``——不属于该动作的边对该动作不可达。
唯一推进入口 ``advance_status`` 把已用于写事实的 ``owner_action`` 原样传下来，
一个参数两处用途，物理上无法不一致。

契约权威定义（state_machines.yaml → objects）：
    Project:        DRAFT→ACTIVE→ON_HOLD；ACTIVE→COMPLETED；ON_HOLD→ACTIVE；
                    DRAFT/ON_HOLD/COMPLETED→ARCHIVED
    ScriptRevision: DRAFT→LOCK_PENDING→LOCKED；DRAFT/LOCK_PENDING/LOCKED→SUPERSEDED
    Episode:        DRAFT→ACTIVE；DRAFT/ACTIVE→ARCHIVED

设计要点（不屎山代码）：
- 纯声明式数据 + 校验函数，无副作用、无框架依赖。
- 每个对象的边表集中在**本模块**，使 contract_check.py 能统一反射比对，
  避免「Project 用 state_machine.py、ScriptRevision 用内嵌转移表」的落点不一致。
- 邻接集（``*_ALLOWED_TRANSITIONS``）是边表的**派生投影**，方向唯一，
  不存在两份真相。
- 终态不可原地复活：终态在边表里没有出边，不是靠额外的 if 拦住。
"""

from __future__ import annotations

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.shared.lifecycle import Lifecycle, Transition
from services.project_planning.domain.enums import (
    EpisodeStatus,
    ProjectStatus,
    ScriptStatus,
)

# --------------------------------------------------------------------------- #
# 拥有各条转移边的动作（唯一来源：契约 objects.<Object>.note 与
# contracts/action_registry/action_registry.yaml）。
# 服务层不再手写这些字面量——两处各写一次就是允许某一处写错。
# --------------------------------------------------------------------------- #
PROJECT_STATUS_CHANGE_ACTION = "PROJECT.STATUS.CHANGE"
EPISODE_STATUS_CHANGE_ACTION = "EPISODE.STATUS.CHANGE"
SCRIPT_IMPORT_ACTION = "SCRIPT.IMPORT"
SCRIPT_LOCK_REQUEST_ACTION = "SCRIPT.LOCK.REQUEST"
SCRIPT_LOCK_APPROVE_ACTION = "SCRIPT.LOCK.APPROVE"

# --------------------------------------------------------------------------- #
# Project：7 条边，全部归 PROJECT.STATUS.CHANGE。
# 目标状态作参数是契约口径（动作说明：「运行时按 Project 转移表校验」），
# 因此本动作拥有该对象的全部边——所有权在这里仍然有实际作用：未来任何
# 新增专属动作（如 PROJECT.ARCHIVE）都必须显式改边表，而不是悄悄共享入口。
# --------------------------------------------------------------------------- #
PROJECT_TRANSITIONS: tuple[Transition[ProjectStatus], ...] = (
    Transition(ProjectStatus.DRAFT, ProjectStatus.ACTIVE, PROJECT_STATUS_CHANGE_ACTION),
    Transition(ProjectStatus.DRAFT, ProjectStatus.ARCHIVED, PROJECT_STATUS_CHANGE_ACTION),
    Transition(ProjectStatus.ACTIVE, ProjectStatus.ON_HOLD, PROJECT_STATUS_CHANGE_ACTION),
    Transition(ProjectStatus.ACTIVE, ProjectStatus.COMPLETED, PROJECT_STATUS_CHANGE_ACTION),
    Transition(ProjectStatus.ON_HOLD, ProjectStatus.ACTIVE, PROJECT_STATUS_CHANGE_ACTION),
    Transition(ProjectStatus.ON_HOLD, ProjectStatus.ARCHIVED, PROJECT_STATUS_CHANGE_ACTION),
    Transition(ProjectStatus.COMPLETED, ProjectStatus.ARCHIVED, PROJECT_STATUS_CHANGE_ACTION),
)

PROJECT_LIFECYCLE = Lifecycle(
    object_name="Project",
    states=tuple(ProjectStatus),
    edges=PROJECT_TRANSITIONS,
)

# --------------------------------------------------------------------------- #
# Episode：3 条边，全部归 EPISODE.STATUS.CHANGE。
# DRAFT 可直接归档（登记错误或撤销）；ARCHIVED 为终态。
# --------------------------------------------------------------------------- #
EPISODE_TRANSITIONS: tuple[Transition[EpisodeStatus], ...] = (
    Transition(EpisodeStatus.DRAFT, EpisodeStatus.ACTIVE, EPISODE_STATUS_CHANGE_ACTION),
    Transition(EpisodeStatus.DRAFT, EpisodeStatus.ARCHIVED, EPISODE_STATUS_CHANGE_ACTION),
    Transition(EpisodeStatus.ACTIVE, EpisodeStatus.ARCHIVED, EPISODE_STATUS_CHANGE_ACTION),
)

EPISODE_LIFECYCLE = Lifecycle(
    object_name="Episode",
    states=tuple(EpisodeStatus),
    edges=EPISODE_TRANSITIONS,
)

# --------------------------------------------------------------------------- #
# ScriptRevision：5 条边，三个动作分领（这是本项目里所有权最有实际后果的对象）。
# - 两步审批流：申请人提交 DRAFT→LOCK_PENDING，批准人 LOCK_PENDING→LOCKED；
# - 版本取代：新版本导入使旧版本 SUPERSEDED（三个来源态同归 SCRIPT.IMPORT）。
# 「批准」与「取代」若共用入口，Agent 就能以「导入新版本」之名把一个待批
# 剧本直接推成终态——所有权判定把这个可能性在结构层封死。
# --------------------------------------------------------------------------- #
SCRIPT_TRANSITIONS: tuple[Transition[ScriptStatus], ...] = (
    Transition(
        ScriptStatus.DRAFT, ScriptStatus.LOCK_PENDING, SCRIPT_LOCK_REQUEST_ACTION
    ),
    Transition(ScriptStatus.LOCK_PENDING, ScriptStatus.LOCKED, SCRIPT_LOCK_APPROVE_ACTION),
    Transition(ScriptStatus.DRAFT, ScriptStatus.SUPERSEDED, SCRIPT_IMPORT_ACTION),
    Transition(ScriptStatus.LOCK_PENDING, ScriptStatus.SUPERSEDED, SCRIPT_IMPORT_ACTION),
    Transition(ScriptStatus.LOCKED, ScriptStatus.SUPERSEDED, SCRIPT_IMPORT_ACTION),
)

SCRIPT_LIFECYCLE = Lifecycle(
    object_name="ScriptRevision",
    states=tuple(ScriptStatus),
    edges=SCRIPT_TRANSITIONS,
)

# --------------------------------------------------------------------------- #
# 结构投影（契约闸门按邻接集比对 YAML）。派生自边表，方向唯一。
# 名字保留不变：contract_check.py::_STATE_MACHINE_SOURCES 按变量名反射加载。
# --------------------------------------------------------------------------- #
ALLOWED_TRANSITIONS: dict[ProjectStatus, frozenset[ProjectStatus]] = (
    PROJECT_LIFECYCLE.allowed_transitions()
)
SCRIPT_ALLOWED_TRANSITIONS: dict[ScriptStatus, frozenset[ScriptStatus]] = (
    SCRIPT_LIFECYCLE.allowed_transitions()
)
EPISODE_ALLOWED_TRANSITIONS: dict[EpisodeStatus, frozenset[EpisodeStatus]] = (
    EPISODE_LIFECYCLE.allowed_transitions()
)

# --------------------------------------------------------------------------- #
# 兼容入口：既有调用方与测试使用「按对象命名」的函数。
# 语义统一为「只判结构，不含所有权」——所有权校验在 edge_owned_by 与聚合根的
# transition_to 上，任何真正的推进都必须经过后者。
# --------------------------------------------------------------------------- #


def can_transition(source: ProjectStatus, target: ProjectStatus) -> bool:
    """判断 Project 的 source -> target 是否为已登记转移。"""
    return PROJECT_LIFECYCLE.edge_for(source, target) is not None


def assert_transition(source: ProjectStatus, target: ProjectStatus) -> Transition:
    """校验 Project 转移结构，未登记则抛 DomainError（31.5）。

    返回该边，便于调用方复用同一条声明（避免「校验一遍、再查一遍」）。
    """
    return PROJECT_LIFECYCLE.assert_registered(source, target)


def can_transition_script(source: ScriptStatus, target: ScriptStatus) -> bool:
    """判断 ScriptRevision 的 source -> target 是否为已登记转移。"""
    return SCRIPT_LIFECYCLE.edge_for(source, target) is not None


def assert_transition_script(
    source: ScriptStatus, target: ScriptStatus
) -> Transition:
    """校验 ScriptRevision 转移结构，未登记则抛 DomainError。"""
    return SCRIPT_LIFECYCLE.assert_registered(source, target)


def can_transition_episode(source: EpisodeStatus, target: EpisodeStatus) -> bool:
    """判断 Episode 的 source -> target 是否为已登记转移。"""
    return EPISODE_LIFECYCLE.edge_for(source, target) is not None


def assert_transition_episode(
    source: EpisodeStatus, target: EpisodeStatus
) -> Transition:
    """校验 Episode 转移结构，未登记则抛 DomainError。"""
    return EPISODE_LIFECYCLE.assert_registered(source, target)


__all__ = [
    "ALLOWED_TRANSITIONS",
    "EPISODE_ALLOWED_TRANSITIONS",
    "EPISODE_LIFECYCLE",
    "EPISODE_STATUS_CHANGE_ACTION",
    "EPISODE_TRANSITIONS",
    "PROJECT_LIFECYCLE",
    "PROJECT_STATUS_CHANGE_ACTION",
    "PROJECT_TRANSITIONS",
    "SCRIPT_ALLOWED_TRANSITIONS",
    "SCRIPT_IMPORT_ACTION",
    "SCRIPT_LIFECYCLE",
    "SCRIPT_LOCK_APPROVE_ACTION",
    "SCRIPT_LOCK_REQUEST_ACTION",
    "SCRIPT_TRANSITIONS",
    "assert_transition",
    "assert_transition_episode",
    "assert_transition_script",
    "can_transition",
    "can_transition_episode",
    "can_transition_script",
]
