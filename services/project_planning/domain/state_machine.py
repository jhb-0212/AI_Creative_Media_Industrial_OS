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

from packages.shared.lifecycle import (
    ChildPrecondition,
    Lifecycle,
    ParentPrecondition,
    Transition,
)
from services.project_planning.domain.enums import (
    ConstraintPackStatus,
    EpisodeStatus,
    GenerationTaskStatus,
    ProductionRouteStatus,
    ProjectStatus,
    ReworkTaskStatus,
    SceneStatus,
    ScriptStatus,
    ShotBudgetRequestStatus,
    ShotDraftStatus,
    ShotRevisionStatus,
    ShotSpecStatus,
    ShotStatus,
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
    # 冻结子集合（子对象 Episode）：COMPLETED/ARCHIVED 均无回到 ACTIVE 的路，
    # 此后登记的子对象会合法落库却永远推不动。逐条显式声明，不用终态推导
    # （COMPLETED 有出边却是冻结态，两者是交叉关系）。迁移自本地归属治理线。
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
    # 冻结子集合（子对象 Scene / ScriptRevision）：归档后不得再登记场次或导入剧本。
    frozen_states=(EpisodeStatus.ARCHIVED,),
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
# A13（ADR-R28）：Scene / Shot / ShotRevision / ShotBreakdownDraft / ReworkTask
#
# 五个对象一次性接通，因为它们是同一条事务链：
#   Shot.required 含 shot_revision_id → 需要 ShotRevision
#   → 唯一入口 SHOT.REVISION.CONFIRM → 输入是 ShotBreakdownDraft
#   → 唯一入口 SHOT.DRAFT.CREATE
# 单独实现任何一环，都会立刻撞上下一环（详见 ADR-R28 §1）。
# --------------------------------------------------------------------------- #
SCENE_STATUS_CHANGE_ACTION = "SCENE.STATUS.CHANGE"
SHOT_STATUS_CHANGE_ACTION = "SHOT.STATUS.CHANGE"
SHOT_REVISION_CONFIRM_ACTION = "SHOT.REVISION.CONFIRM"
SHOT_DRAFT_CONFIRM_ACTION = "SHOT.DRAFT.CONFIRM"
SHOT_DRAFT_RETURN_ACTION = "SHOT.DRAFT.RETURN"
SHOT_DRAFT_DISCARD_ACTION = "SHOT.DRAFT.DISCARD"
REWORK_TASK_STATUS_CHANGE_ACTION = "REWORK.TASK.STATUS.CHANGE"
# A22：ShotSpec 的唯一转移边（DRAFT → LOCKED）由 HIGH 风险的锁定动作独占。
# 创建动作不拥有任何边——它不改变状态，只决定出生点。
SHOT_SPEC_LOCK_ACTION = "SHOTSPEC.LOCK"

# A23：ConstraintPack 的两类边分属两个动作，语义差别是刻意的：
# - DRAFT → ACTIVE 归 CONSTRAINT.PACK.LOCK（HIGH，导演/授权 TD）；
# - ACTIVE → STALE / SUPERSEDED / REVOKED 归 CONSTRAINT.PACK.STATUS.CHANGE。
#
# 为什么治理边不能也归 LOCK：LOCK 的回答是「这份包的锁定义务已尽、可以作生成
# 输入」，治理边回答的是「这份包此后还能不能被用」。共用一个入口会让「锁一份
# 新包」与「撤销一份已生效的包」成为同一种通行证——恰恰是 N-07 修掉的形态。
#
# STATUS.CHANGE 已在契约登记（A23，`not_implemented`），运行时本批不实现：
# 三条治理边的下游消费者（生成路线、STALE 传播）尚不存在，现在实现只能得到
# 「能改状态、但没人读」的边。逐边债务登记在 state_machine_gaps.yaml。
CONSTRAINT_PACK_LOCK_ACTION = "CONSTRAINT.PACK.LOCK"
CONSTRAINT_PACK_STATUS_CHANGE_ACTION = "CONSTRAINT.PACK.STATUS.CHANGE"

# Scene：3 条边，全部归 SCENE.STATUS.CHANGE（与 Episode 同构）。
#
# SC-5 的跨聚合前置条件在这里声明（ADR-R28 §6.3），两侧成对：
# - 父侧（归档）：其下不得存在 IN_PRODUCTION 的镜头；
# - 子侧（Shot 进入在产）：父场次不得已归档。
#
# 为什么声明成边属性而不是写进服务层的 if：手写分支只覆盖当时想到的边，
# 而进入 IN_PRODUCTION 的边有三条（NOT_STARTED / BLOCKED / PRODUCTION_COMPLETE）。
# 只要其中一条漏了屏障，不变量就被静默绕过，且没有任何闸门会红。挂在边上之后，
# 「要不要屏障」是每条边的显式决定，服务层按边表派生触发条件。
SCENE_ARCHIVE_CHILD_PRECONDITION = ChildPrecondition(
    object_name="Shot",
    forbidden_status=ShotStatus.IN_PRODUCTION.value,
    message="场次下仍有 IN_PRODUCTION 的镜头，拒绝归档（SC-5）",
)
SHOT_IN_PRODUCTION_PARENT_PRECONDITION = ParentPrecondition(
    object_name="Scene",
    forbidden_status=SceneStatus.ARCHIVED.value,
    message="父场次已归档，镜头不可进入 IN_PRODUCTION（SC-5 的并发侧）",
)

SCENE_TRANSITIONS: tuple[Transition[SceneStatus], ...] = (
    Transition(SceneStatus.DRAFT, SceneStatus.ACTIVE, SCENE_STATUS_CHANGE_ACTION),
    Transition(
        SceneStatus.DRAFT,
        SceneStatus.ARCHIVED,
        SCENE_STATUS_CHANGE_ACTION,
        child_precondition=SCENE_ARCHIVE_CHILD_PRECONDITION,
    ),
    Transition(
        SceneStatus.ACTIVE,
        SceneStatus.ARCHIVED,
        SCENE_STATUS_CHANGE_ACTION,
        child_precondition=SCENE_ARCHIVE_CHILD_PRECONDITION,
    ),
)

SCENE_LIFECYCLE = Lifecycle(
    object_name="Scene",
    states=tuple(SceneStatus),
    edges=SCENE_TRANSITIONS,
    # 冻结子集合（子对象 Shot）：归档后不得再挂新镜头。与 SC-5 的 A 侧守卫
    # （归档时不得有在制镜头）方向相反、互不替代。迁移自本地归属治理线。
    frozen_states=(SceneStatus.ARCHIVED,),
)

# Shot：10 条边，全部归 SHOT.STATUS.CHANGE。
# 这里只有一个动作，但所有权判定照常执行——它的价值在「未来新增专属动作
# （如 SHOT.CANCEL）时必须显式改边表」，而不是让新动作悄悄共享入口。
# BLOCKED→NOT_STARTED 是合法回退：阻塞解除后可能整体重排而未启动。
#
# 三条进入 IN_PRODUCTION 的边全部带父场次屏障（SC-5 的并发侧）。三条都要：
# 屏障的语义是「进入在产」，与从哪个状态进来无关；只给其中一条加，等于允许
# 从另外两条绕过归档判定。
SHOT_TRANSITIONS: tuple[Transition[ShotStatus], ...] = (
    Transition(
        ShotStatus.NOT_STARTED,
        ShotStatus.IN_PRODUCTION,
        SHOT_STATUS_CHANGE_ACTION,
        parent_precondition=SHOT_IN_PRODUCTION_PARENT_PRECONDITION,
    ),
    Transition(ShotStatus.NOT_STARTED, ShotStatus.BLOCKED, SHOT_STATUS_CHANGE_ACTION),
    Transition(ShotStatus.NOT_STARTED, ShotStatus.CANCELLED, SHOT_STATUS_CHANGE_ACTION),
    Transition(ShotStatus.IN_PRODUCTION, ShotStatus.PRODUCTION_COMPLETE, SHOT_STATUS_CHANGE_ACTION),
    Transition(ShotStatus.IN_PRODUCTION, ShotStatus.BLOCKED, SHOT_STATUS_CHANGE_ACTION),
    Transition(ShotStatus.IN_PRODUCTION, ShotStatus.CANCELLED, SHOT_STATUS_CHANGE_ACTION),
    Transition(ShotStatus.BLOCKED, ShotStatus.NOT_STARTED, SHOT_STATUS_CHANGE_ACTION),
    Transition(
        ShotStatus.BLOCKED,
        ShotStatus.IN_PRODUCTION,
        SHOT_STATUS_CHANGE_ACTION,
        parent_precondition=SHOT_IN_PRODUCTION_PARENT_PRECONDITION,
    ),
    Transition(ShotStatus.BLOCKED, ShotStatus.CANCELLED, SHOT_STATUS_CHANGE_ACTION),
    Transition(
        ShotStatus.PRODUCTION_COMPLETE,
        ShotStatus.IN_PRODUCTION,
        SHOT_STATUS_CHANGE_ACTION,
        parent_precondition=SHOT_IN_PRODUCTION_PARENT_PRECONDITION,
    ),
)

SHOT_LIFECYCLE = Lifecycle(
    object_name="Shot",
    states=tuple(ShotStatus),
    edges=SHOT_TRANSITIONS,
    # 有回边（BLOCKED↔NOT_STARTED、PRODUCTION_COMPLETE→IN_PRODUCTION），无入边
    # 推导会得到空集。正式镜头只由 SHOT.REVISION.CONFIRM 创建，那一刻固定在
    # NOT_STARTED——领域事实，不是从图上猜的。迁移自本地归属治理线。
    initial_states=(ShotStatus.NOT_STARTED,),
)

# ShotRevision：1 条边。新修订使旧修订 SUPERSEDED，旧版本不可原地覆盖。
SHOT_REVISION_TRANSITIONS: tuple[Transition[ShotRevisionStatus], ...] = (
    Transition(
        ShotRevisionStatus.ACTIVE,
        ShotRevisionStatus.SUPERSEDED,
        SHOT_REVISION_CONFIRM_ACTION,
    ),
)

SHOT_REVISION_LIFECYCLE = Lifecycle(
    object_name="ShotRevision",
    states=tuple(ShotRevisionStatus),
    edges=SHOT_REVISION_TRANSITIONS,
    # ACTIVE 有入边（返工取代）但确是首版初始态，不能按无入边推导。
    initial_states=(ShotRevisionStatus.ACTIVE,),
    # 冻结子集合（子对象 ShotSpec）：SUPERSEDED 之后不得再为它登记新规格。
    #
    # 为什么这条必须声明而不是留给服务层判断：规格是「为生产某条修订而定的拍摄
    # 参数」。修订一旦被取代，其规格只作为历史证据保留；此后为它补登一份新规格，
    # 会产生一个永远不会被生产使用、却与「有效规格集」难以区分的对象。闸门也正是
    # 在这里要求表态——新增子表就必须回答「父对象什么状态下不再长」。
    frozen_states=(ShotRevisionStatus.SUPERSEDED,),
)

# ShotBreakdownDraft：5 条边，三个动作分领。
# CONFIRM 拥有两条边（认领导致 IN_REVIEW，再确认到 CONFIRMED）：认领与确认是
# 同一用例的两阶段，一次调用在同一事务内连走两跳，两条事实共用同一个
# owner_action。「认领」单独占一个动作位没有业务语义，它只是并发锁。
SHOT_DRAFT_TRANSITIONS: tuple[Transition[ShotDraftStatus], ...] = (
    Transition(
        ShotDraftStatus.PENDING_CONFIRM, ShotDraftStatus.IN_REVIEW, SHOT_DRAFT_CONFIRM_ACTION
    ),
    Transition(
        ShotDraftStatus.IN_REVIEW, ShotDraftStatus.CONFIRMED, SHOT_DRAFT_CONFIRM_ACTION
    ),
    Transition(
        ShotDraftStatus.IN_REVIEW, ShotDraftStatus.RETURNED, SHOT_DRAFT_RETURN_ACTION
    ),
    Transition(
        ShotDraftStatus.PENDING_CONFIRM, ShotDraftStatus.DISCARDED, SHOT_DRAFT_DISCARD_ACTION
    ),
    Transition(
        ShotDraftStatus.IN_REVIEW, ShotDraftStatus.DISCARDED, SHOT_DRAFT_DISCARD_ACTION
    ),
)

SHOT_DRAFT_LIFECYCLE = Lifecycle(
    object_name="ShotBreakdownDraft",
    states=tuple(ShotDraftStatus),
    edges=SHOT_DRAFT_TRANSITIONS,
)

# ReworkTask：4 条边。REJECTED 无入边——它是合法状态值，但当前没有任何
# 授权转移，按 31.5 未登记转移一律拒绝（不是「未实现所以放行」）。
REWORK_TASK_TRANSITIONS: tuple[Transition[ReworkTaskStatus], ...] = (
    Transition(ReworkTaskStatus.OPEN, ReworkTaskStatus.IN_PROGRESS, REWORK_TASK_STATUS_CHANGE_ACTION),
    Transition(ReworkTaskStatus.IN_PROGRESS, ReworkTaskStatus.DONE, REWORK_TASK_STATUS_CHANGE_ACTION),
    Transition(ReworkTaskStatus.DONE, ReworkTaskStatus.VERIFYING, REWORK_TASK_STATUS_CHANGE_ACTION),
    Transition(ReworkTaskStatus.VERIFYING, ReworkTaskStatus.CLOSED, REWORK_TASK_STATUS_CHANGE_ACTION),
)

REWORK_TASK_LIFECYCLE = Lifecycle(
    object_name="ReworkTask",
    states=tuple(ReworkTaskStatus),
    edges=REWORK_TASK_TRANSITIONS,
)

# 结构投影（契约闸门按邻接集比对 YAML），派生自边表，方向唯一。
SCENE_ALLOWED_TRANSITIONS: dict[SceneStatus, frozenset[SceneStatus]] = (
    SCENE_LIFECYCLE.allowed_transitions()
)
SHOT_ALLOWED_TRANSITIONS: dict[ShotStatus, frozenset[ShotStatus]] = (
    SHOT_LIFECYCLE.allowed_transitions()
)
SHOT_REVISION_ALLOWED_TRANSITIONS: dict[ShotRevisionStatus, frozenset[ShotRevisionStatus]] = (
    SHOT_REVISION_LIFECYCLE.allowed_transitions()
)
SHOT_DRAFT_ALLOWED_TRANSITIONS: dict[ShotDraftStatus, frozenset[ShotDraftStatus]] = (
    SHOT_DRAFT_LIFECYCLE.allowed_transitions()
)
REWORK_TASK_ALLOWED_TRANSITIONS: dict[ReworkTaskStatus, frozenset[ReworkTaskStatus]] = (
    REWORK_TASK_LIFECYCLE.allowed_transitions()
)


# ShotSpec（A21 契约 / A22 实现）：一条边，归 SHOTSPEC.LOCK。
#
# 为什么创建（SHOTSPEC.CREATE）不在边表里：创建不是转移，对象一诞生就是 DRAFT
# （initial_states 显式声明）。把 CREATE 塞进边表会让「没有源状态的边」无法表达，
# 也会让所有权判定同时回答两个问题（谁能建 / 谁能改），而它们本就是两个动作。
#
# LOCKED 在边表里没有出边 —— 终态不可原地复活由**结构**保证，不由 if 保证。
SHOT_SPEC_TRANSITIONS: tuple[Transition[ShotSpecStatus], ...] = (
    Transition(
        ShotSpecStatus.DRAFT,
        ShotSpecStatus.LOCKED,
        SHOT_SPEC_LOCK_ACTION,
    ),
)

SHOT_SPEC_LIFECYCLE = Lifecycle(
    object_name="ShotSpec",
    states=tuple(ShotSpecStatus),
    edges=SHOT_SPEC_TRANSITIONS,
    # DRAFT 无入边，本可从图上推导；显式声明是为了表达「创建即草稿」这个领域
    # 事实——推导只回答「图上没有入边」，不回答「它是不是一个合法的出生点」。
    initial_states=(ShotSpecStatus.DRAFT,),
    # 叶子对象：没有子对象挂在规格下，故不冻结任何状态（契约 frozen_states: []）。
    # 显式写成空元组而不是省略：省略与「声明为空」在契约比对里必须同形。
    frozen_states=(),
)

# 结构投影（契约闸门按邻接集比对 YAML），派生自边表，方向唯一。
SHOT_SPEC_ALLOWED_TRANSITIONS: dict[ShotSpecStatus, frozenset[ShotSpecStatus]] = (
    SHOT_SPEC_LIFECYCLE.allowed_transitions()
)


# --------------------------------------------------------------------------- #
# ConstraintPack：4 条边，两个动作分领（A23）。
#
# 契约（7.9）：「DRAFT → ACTIVE；ACTIVE 后可进入 STALE / SUPERSEDED / REVOKED」。
# 四态之后没有任何出边——STALE / SUPERSEDED / REVOKED 都是治理结论，不可原地
# 恢复（恢复须新建版本重走审批）；ACTIVE 是唯一可作生成输入的状态。
#
# 为什么 ACTIVE 到三个治理态是三条边而不是一个「STATUS.CHANGE 万能边」：
# 每条边的前置依据不同（撤销要有撤销依据、失效要指认失效源、取代要有新版引用），
# 合成一条会让「凭什么进这个状态」在边表里消失。边表是准入凭据的来源，
# 合并之后就只剩服务层记得。
# --------------------------------------------------------------------------- #
CONSTRAINT_PACK_TRANSITIONS: tuple[Transition[ConstraintPackStatus], ...] = (
    Transition(
        ConstraintPackStatus.DRAFT,
        ConstraintPackStatus.ACTIVE,
        CONSTRAINT_PACK_LOCK_ACTION,
    ),
    Transition(
        ConstraintPackStatus.ACTIVE,
        ConstraintPackStatus.STALE,
        CONSTRAINT_PACK_STATUS_CHANGE_ACTION,
    ),
    Transition(
        ConstraintPackStatus.ACTIVE,
        ConstraintPackStatus.SUPERSEDED,
        CONSTRAINT_PACK_STATUS_CHANGE_ACTION,
    ),
    Transition(
        ConstraintPackStatus.ACTIVE,
        ConstraintPackStatus.REVOKED,
        CONSTRAINT_PACK_STATUS_CHANGE_ACTION,
    ),
)

CONSTRAINT_PACK_LIFECYCLE = Lifecycle(
    object_name="ConstraintPack",
    states=tuple(ConstraintPackStatus),
    edges=CONSTRAINT_PACK_TRANSITIONS,
    # 创建即草稿：与 ShotSpec 同形，显式声明而不是靠图上无入边推导。
    initial_states=(ConstraintPackStatus.DRAFT,),
    # 叶子对象：没有子对象挂在约束包下（约束条目是包内 JSON 数组，不是独立对象）。
    frozen_states=(),
)

CONSTRAINT_PACK_ALLOWED_TRANSITIONS: dict[
    ConstraintPackStatus, frozenset[ConstraintPackStatus]
] = CONSTRAINT_PACK_LIFECYCLE.allowed_transitions()


# --------------------------------------------------------------------------- #
# R16-D3：生产链与预算 MVP 的三个生命周期对象。
# - ProductionRoute：VERIFIED → REVOKED（一条边归 PRODUCTION.ROUTE.SELECT；
#   创建即 VERIFIED，契约「路线须处于 VERIFIED」由出生点满足）。
# - GenerationTask：REQUESTED → SUCCEEDED / FAILED（MVP 同步执行；RUNNING 等
#   异步状态归 Phase 2/3 Worker，无 MVP 动作拥有这些边，不建模以免制造债务）。
# - ShotBudgetRequest：DRAFT → APPROVED / REJECTED（两步审批；APPROVED 时同一
#   事务原子追加 BudgetLedger 流水）。
# 三者的边全部由 MVP 动作拥有，不存在「可达但无入口」的边。
# --------------------------------------------------------------------------- #
PRODUCTION_ROUTE_SELECT_ACTION = "PRODUCTION.ROUTE.SELECT"
GENERATION_REQUEST_ACTION = "GENERATION.REQUEST"
BUDGET_SHOT_APPROVE_ACTION = "BUDGET.SHOT.APPROVE"

PRODUCTION_ROUTE_TRANSITIONS: tuple[Transition[ProductionRouteStatus], ...] = (
    Transition(
        ProductionRouteStatus.VERIFIED,
        ProductionRouteStatus.REVOKED,
        PRODUCTION_ROUTE_SELECT_ACTION,
    ),
)

PRODUCTION_ROUTE_LIFECYCLE = Lifecycle(
    object_name="ProductionRoute",
    states=tuple(ProductionRouteStatus),
    edges=PRODUCTION_ROUTE_TRANSITIONS,
    initial_states=(ProductionRouteStatus.VERIFIED,),
    frozen_states=(),
)

PRODUCTION_ROUTE_ALLOWED_TRANSITIONS: dict[
    ProductionRouteStatus, frozenset[ProductionRouteStatus]
] = PRODUCTION_ROUTE_LIFECYCLE.allowed_transitions()

GENERATION_TASK_TRANSITIONS: tuple[Transition[GenerationTaskStatus], ...] = (
    Transition(
        GenerationTaskStatus.REQUESTED,
        GenerationTaskStatus.SUCCEEDED,
        GENERATION_REQUEST_ACTION,
    ),
    Transition(
        GenerationTaskStatus.REQUESTED,
        GenerationTaskStatus.FAILED,
        GENERATION_REQUEST_ACTION,
    ),
)

GENERATION_TASK_LIFECYCLE = Lifecycle(
    object_name="GenerationTask",
    states=tuple(GenerationTaskStatus),
    edges=GENERATION_TASK_TRANSITIONS,
    initial_states=(GenerationTaskStatus.REQUESTED,),
    frozen_states=(),
)

GENERATION_TASK_ALLOWED_TRANSITIONS: dict[
    GenerationTaskStatus, frozenset[GenerationTaskStatus]
] = GENERATION_TASK_LIFECYCLE.allowed_transitions()

SHOT_BUDGET_REQUEST_TRANSITIONS: tuple[
    Transition[ShotBudgetRequestStatus], ...
] = (
    Transition(
        ShotBudgetRequestStatus.DRAFT,
        ShotBudgetRequestStatus.APPROVED,
        BUDGET_SHOT_APPROVE_ACTION,
    ),
    Transition(
        ShotBudgetRequestStatus.DRAFT,
        ShotBudgetRequestStatus.REJECTED,
        BUDGET_SHOT_APPROVE_ACTION,
    ),
)

SHOT_BUDGET_REQUEST_LIFECYCLE = Lifecycle(
    object_name="ShotBudgetRequest",
    states=tuple(ShotBudgetRequestStatus),
    edges=SHOT_BUDGET_REQUEST_TRANSITIONS,
    initial_states=(ShotBudgetRequestStatus.DRAFT,),
    frozen_states=(),
)

SHOT_BUDGET_REQUEST_ALLOWED_TRANSITIONS: dict[
    ShotBudgetRequestStatus, frozenset[ShotBudgetRequestStatus]
] = SHOT_BUDGET_REQUEST_LIFECYCLE.allowed_transitions()


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


__all__ = [
    "ALLOWED_TRANSITIONS",
    "BUDGET_SHOT_APPROVE_ACTION",
    "CONSTRAINT_PACK_ALLOWED_TRANSITIONS",
    "CONSTRAINT_PACK_LIFECYCLE",
    "CONSTRAINT_PACK_LOCK_ACTION",
    "CONSTRAINT_PACK_STATUS_CHANGE_ACTION",
    "CONSTRAINT_PACK_TRANSITIONS",
    "EPISODE_ALLOWED_TRANSITIONS",
    "EPISODE_LIFECYCLE",
    "EPISODE_STATUS_CHANGE_ACTION",
    "EPISODE_TRANSITIONS",
    "GENERATION_REQUEST_ACTION",
    "GENERATION_TASK_ALLOWED_TRANSITIONS",
    "GENERATION_TASK_LIFECYCLE",
    "GENERATION_TASK_TRANSITIONS",
    "PRODUCTION_ROUTE_ALLOWED_TRANSITIONS",
    "PRODUCTION_ROUTE_LIFECYCLE",
    "PRODUCTION_ROUTE_SELECT_ACTION",
    "PRODUCTION_ROUTE_TRANSITIONS",
    "PROJECT_LIFECYCLE",
    "PROJECT_STATUS_CHANGE_ACTION",
    "PROJECT_TRANSITIONS",
    "REWORK_TASK_ALLOWED_TRANSITIONS",
    "REWORK_TASK_LIFECYCLE",
    "REWORK_TASK_STATUS_CHANGE_ACTION",
    "REWORK_TASK_TRANSITIONS",
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
    "SHOT_ALLOWED_TRANSITIONS",
    "SHOT_BUDGET_REQUEST_ALLOWED_TRANSITIONS",
    "SHOT_BUDGET_REQUEST_LIFECYCLE",
    "SHOT_BUDGET_REQUEST_TRANSITIONS",
    "SHOT_DRAFT_ALLOWED_TRANSITIONS",
    "SHOT_DRAFT_CONFIRM_ACTION",
    "SHOT_DRAFT_DISCARD_ACTION",
    "SHOT_DRAFT_LIFECYCLE",
    "SHOT_DRAFT_RETURN_ACTION",
    "SHOT_DRAFT_TRANSITIONS",
    "SHOT_LIFECYCLE",
    "SHOT_REVISION_ALLOWED_TRANSITIONS",
    "SHOT_REVISION_CONFIRM_ACTION",
    "SHOT_REVISION_LIFECYCLE",
    "SHOT_REVISION_TRANSITIONS",
    "SHOT_SPEC_ALLOWED_TRANSITIONS",
    "SHOT_SPEC_LIFECYCLE",
    "SHOT_SPEC_LOCK_ACTION",
    "SHOT_SPEC_TRANSITIONS",
    "SHOT_STATUS_CHANGE_ACTION",
    "SHOT_TRANSITIONS",
    "assert_transition",
    "assert_transition_episode",
    "assert_transition_script",
    "can_transition",
    "can_transition_episode",
    "can_transition_script",
]
