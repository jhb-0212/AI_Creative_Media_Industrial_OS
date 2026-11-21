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

from packages.shared.lifecycle import Lifecycle, Transition
from services.project_planning.domain.enums import (
    EpisodeStatus,
    ProjectStatus,
    SceneStatus,
    ScriptStatus,
)

# --------------------------------------------------------------------------- #
# 拥有各条转移边的动作（唯一来源：契约 objects.<Object>.note 与
# contracts/action_registry/action_registry.yaml）。
# 服务层不再手写这些字面量——两处各写一次就是允许某一处写错。
# --------------------------------------------------------------------------- #
PROJECT_STATUS_CHANGE_ACTION = "PROJECT.STATUS.CHANGE"
EPISODE_STATUS_CHANGE_ACTION = "EPISODE.STATUS.CHANGE"
SCENE_STATUS_CHANGE_ACTION = "SCENE.STATUS.CHANGE"
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
    # 冻结子集合（Project 的子对象是 Episode）：项目一旦「内容定稿」，不得再登记集数。
    #
    # **两态都冻结，理由不是「终态自然该冻结」，而是状态图本身给不出回路**：
    # ``COMPLETED`` 只有 ``COMPLETED -> ARCHIVED`` 一条出边，没有回到 ``ACTIVE``
    # 的路。也就是说「项目已完成」在状态图上等价于「内容定稿」，此后任何新增
    # 内容都不可能再被推进——它会合法地落在库里，然后永久停在 ``DRAFT``。
    # 这类「能写进去、永远推不动」的孤儿数据，正是「只拦一半」的典型产物：
    # 归档侧拦住了，完成侧放行，调用方拿到的是一次 201 和一份死数据。
    #
    # 为什么不写成 ``terminal_states()``：见 ``packages/shared/lifecycle.py`` 的
    # 说明——终态与冻结态是交叉关系（``COMPLETED`` 有出边却不是终态），互相推导
    # 会让新增状态静默继承一个没人讨论过的语义。这里逐条显式声明。
    frozen_states=(ProjectStatus.COMPLETED, ProjectStatus.ARCHIVED),
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
    # 冻结子集合（Episode 的子对象是 Scene 与 ScriptRevision）：归档后不得再登记
    # 场次，也不得再导入剧本版本。两条路径从同一处派生，不各写一个 if。
    frozen_states=(EpisodeStatus.ARCHIVED,),
)

# --------------------------------------------------------------------------- #
# Scene：3 条边，全部归 SCENE.STATUS.CHANGE。
# 形状与 Episode 完全一致（契约 `source_kind: derived_from_parent_lifecycle`）：
# 同层级聚合若各有一套命名不同的状态，跨层校验就无法复用同一套判定。
# ARCHIVED 为终态；归档另有数据库守卫检查「其下不得有在制镜头」（ADR-R27/Q3），
# 那条是跨表存在性不变量，状态机表达不了，故不在这里重复。
# --------------------------------------------------------------------------- #
SCENE_TRANSITIONS: tuple[Transition[SceneStatus], ...] = (
    Transition(SceneStatus.DRAFT, SceneStatus.ACTIVE, SCENE_STATUS_CHANGE_ACTION),
    Transition(SceneStatus.DRAFT, SceneStatus.ARCHIVED, SCENE_STATUS_CHANGE_ACTION),
    Transition(SceneStatus.ACTIVE, SceneStatus.ARCHIVED, SCENE_STATUS_CHANGE_ACTION),
)

SCENE_LIFECYCLE = Lifecycle(
    object_name="Scene",
    states=tuple(SceneStatus),
    edges=SCENE_TRANSITIONS,
    # 冻结子集合（Scene 的子对象是 Shot）：归档后不得再挂新镜头。
    # 这条与数据库侧的 A 侧守卫（归档时不得有在制镜头）方向相反、互不替代：
    # 前者拦「以后还能不能长」，后者拦「现在还有没有在制」。
    frozen_states=(SceneStatus.ARCHIVED,),
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
SCENE_ALLOWED_TRANSITIONS: dict[SceneStatus, frozenset[SceneStatus]] = (
    SCENE_LIFECYCLE.allowed_transitions()
)

# --------------------------------------------------------------------------- #
# 兼容入口：既有调用方与测试使用「按对象命名」的函数。
# 语义统一为「只判结构，不含所有权」——所有权校验在 edge_owned_by 与聚合根的
# transition_to 上，任何真正的推进都必须经过后者。
# --------------------------------------------------------------------------- #


def can_transition(source: ProjectStatus, target: ProjectStatus) -> bool:
    """判断 Project 的 source -> target 是否为已登记转移。"""
    return PROJECT_LIFECYCLE.edge_for(source, target) is not None


def assert_transition(
    source: ProjectStatus, target: ProjectStatus
) -> Transition[ProjectStatus]:
    """校验 Project 转移结构，未登记则抛 DomainError（31.5）。

    返回该边，便于调用方复用同一条声明（避免「校验一遍、再查一遍」）。
    """
    return PROJECT_LIFECYCLE.assert_registered(source, target)


def can_transition_script(source: ScriptStatus, target: ScriptStatus) -> bool:
    """判断 ScriptRevision 的 source -> target 是否为已登记转移。"""
    return SCRIPT_LIFECYCLE.edge_for(source, target) is not None


def assert_transition_script(
    source: ScriptStatus, target: ScriptStatus
) -> Transition[ScriptStatus]:
    """校验 ScriptRevision 转移结构，未登记则抛 DomainError。"""
    return SCRIPT_LIFECYCLE.assert_registered(source, target)


def can_transition_episode(source: EpisodeStatus, target: EpisodeStatus) -> bool:
    """判断 Episode 的 source -> target 是否为已登记转移。"""
    return EPISODE_LIFECYCLE.edge_for(source, target) is not None


def assert_transition_episode(
    source: EpisodeStatus, target: EpisodeStatus
) -> Transition[EpisodeStatus]:
    """校验 Episode 转移结构，未登记则抛 DomainError。"""
    return EPISODE_LIFECYCLE.assert_registered(source, target)


def can_transition_scene(source: SceneStatus, target: SceneStatus) -> bool:
    """判断 Scene 的 source -> target 是否为已登记转移。"""
    return SCENE_LIFECYCLE.edge_for(source, target) is not None


def assert_transition_scene(
    source: SceneStatus, target: SceneStatus
) -> Transition[SceneStatus]:
    """校验 Scene 转移结构，未登记则抛 DomainError。"""
    return SCENE_LIFECYCLE.assert_registered(source, target)


__all__ = [
    "ALLOWED_TRANSITIONS",
    "EPISODE_ALLOWED_TRANSITIONS",
    "EPISODE_LIFECYCLE",
    "EPISODE_STATUS_CHANGE_ACTION",
    "EPISODE_TRANSITIONS",
    "PROJECT_LIFECYCLE",
    "PROJECT_STATUS_CHANGE_ACTION",
    "PROJECT_TRANSITIONS",
    "SCENE_ALLOWED_TRANSITIONS",
    "SCENE_LIFECYCLE",
    "SCENE_STATUS_CHANGE_ACTION",
    "SCENE_TRANSITIONS",
    "SCRIPT_ALLOWED_TRANSITIONS",
    "SCRIPT_IMPORT_ACTION",
    "SCRIPT_LIFECYCLE",
    "SCRIPT_LOCK_APPROVE_ACTION",
    "SCRIPT_LOCK_REQUEST_ACTION",
    "SCRIPT_TRANSITIONS",
    "assert_transition",
    "assert_transition_episode",
    "assert_transition_scene",
    "assert_transition_script",
    "can_transition",
    "can_transition_episode",
    "can_transition_scene",
    "can_transition_script",
]
