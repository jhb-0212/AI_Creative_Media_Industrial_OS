"""状态机边表的唯一形态：结构 + 所有者 + 准入构件（N-07 的根治落点）。

为什么需要把边表提到共享层（根因，不是为抽象而抽象）
--------------------------------------------------
整改前，两个域对「一条转移边」有不同的形态：

* 资产域：``Transition(source, target, owner_action, guards)`` + ``edge_owned_by()``，
  所有权与守卫**在推进路径上真正执行**；
* 项目域：``{source: frozenset(target)}`` 邻接集，``owner_action`` 只是一个字符串
  参数，被原样写进 ``audit.status_transitions`` 与 outbox 事件——即「边的所有者被
  **记录**，但从未被**执行**」。

后果不是风格问题：项目域任何能拿到聚合的调用方，只要在 ``advance_status`` 里填一个
看起来对的 ``owner_action``，就能走任意一条**已登记**的边。``SCRIPT.SUPERSEDED``
（归 ``SCRIPT.IMPORT``）与 ``SCRIPT.LOCKED``（归 ``SCRIPT.LOCK.APPROVE``）在运行时是
同一种通行证，区别只是一行文本。审计表会忠实地把「谁走的」记成调用方自称的那个人。

所以修法必须让「记录的动作」与「准入的动作」是**同一个值**，而不是两次填写：

1. 边表只有一种形态（本模块 ``Lifecycle``），所有域都用它；
2. 域聚合的 ``transition_to(target, audit_ref, action)`` 必须接收 action，
   并在内部调 ``edge_owned_by`` —— 不属于该动作的边对该动作不可达；
3. 唯一推进入口 ``advance_status`` 把它已用于写事实的 ``owner_action``
   原样传给 ``transition_to``。一个参数，两处用途，物理上无法不一致。

设计边界
--------
- 纯声明式数据 + 纯函数判定，无 IO、无框架依赖、无全局可变状态。
- 结构不变量（重复边、未知状态、空 owner）在**构造时**校验：边表是 import 期常量，
  让脏边表在进程启动时炸，而不是等某条路径走到才发现。
- ``guards`` 是**不透明构件**：共享层只搬运、不解释。资产域的证据/职责分离
  守卫依赖 ``AssetVersion`` 与一次 QC 计数查询，项目域当前无边级守卫；为它们
  编一个只覆盖共同字段的 Protocol，等于让共享层假装理解它并不理解的东西。
  共享层只保证「守卫挂在边上」这一形态——任何推进路径都必须带着这条边的守卫走。
- 本模块不引入 Manager / Registry 单例：``Lifecycle`` 是无状态值对象，
  每个域在自己的 ``state_machine.py`` 里构造一个模块级常量。

为什么「冻结子集合」是状态的属性，而不是动作的属性
------------------------------------------------
「这一支不再长了」是关于**父对象处于某状态时的性质**，不是关于「某个动作是否
允许」的性质。把它记在动作上，就得在每一条「在父对象下创建子对象」的路径上各写
一次判断——而路径数量会增长，漏掉的那一条不会报错，它只是不在那里（本项目在
``qc_reports``/``episodes``/``OWNERSHIP_STACK`` 上已三次栽在同一形态上）。

记在状态上则相反：四条创建路径从同一个 ``is_frozen()`` 派生，第五张子表接入时
不需要改判断，只需要闸门校验「有子表引用的父对象必须声明 ``frozen_states``」。

**刻意不自动推导为 ``terminal_states()``**：终态与冻结态不等价。
``Project.COMPLETED`` 不是终态（仍有 ``COMPLETED -> ARCHIVED`` 边），但「项目已完成」
同样意味着不该再长出新集数。若默认取终态集合，新增一个状态时就会静默继承一个
没人想过的语义——那正是「清单不会自己举手」的翻版。故默认为空元组，必须显式声明。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from packages.errors.error_codes import DomainError, ErrorRegistry


@dataclass(frozen=True)
class Transition[StatusT]:
    """一条已登记的转移边：结构 + 所有者 + 准入构件。

    ``owner_action`` 是**准入凭据**，不是标签：``Lifecycle.edge_owned_by()`` 会拿
    请求方的 action 与它比对，不等即拒。它同时被写进迁移事实（``owner_action`` 列）
    与事件 payload——同一份声明既约束谁能走，也回答「谁走了」。

    ``guards`` 的类型是 ``tuple[Any, ...]``：共享层**不解释**守卫，只保证「守卫挂在
    边上」这一形态。资产域的守卫依赖 ``AssetVersion`` 与一次 QC 计数查询，项目域
    当前无边级守卫；为它们编一个只覆盖共同字段的 Protocol，会让共享层假装理解
    它并不理解的东西，也会让真正的守卫类型在调用点被擦除。不透明比假装更诚实。
    """

    source: StatusT
    target: StatusT
    owner_action: str
    guards: tuple[Any, ...] = ()

    @property
    def label(self) -> str:
        """人能读的边标签（错误信息用）。"""
        return f"{_status_value(self.source)} -> {_status_value(self.target)}"

    @property
    def key(self) -> tuple[StatusT, StatusT]:
        """结构键：用于唯一性校验与索引。"""
        return (self.source, self.target)


@dataclass(frozen=True)
class Lifecycle[StatusT]:
    """一个对象的状态机：全状态集 + 边表（唯一权威）。

    ``states`` 必填且必须覆盖每条边的两端：漏一个状态，邻接集投影就会缺一行，
    而契约闸门按邻接集比对，缺行会被读成「实现少了这条边」——报错位置与真实
    原因（枚举漏写）相距很远。构造时校验把这个距离压到零。
    """

    object_name: str
    states: tuple[StatusT, ...]
    edges: tuple[Transition[StatusT], ...]
    #: 处于这些状态时，**不得再新增子对象**。默认空表示「本对象不冻结子集合」。
    #:
    #: 注意这不是 ``terminal_states()`` 的便捷别名：终态必然不该长子对象，但
    #: 「不该长子对象」的状态未必是终态（``Project.COMPLETED`` 仍有出边）。
    #: 两者是交叉关系，不能互相推导。
    frozen_states: tuple[StatusT, ...] = ()

    def __post_init__(self) -> None:
        problems = validate_lifecycle(
            self.object_name, self.states, self.edges, self.frozen_states
        )
        if problems:
            raise ValueError(
                f"{self.object_name} 状态机边表非法：" + "；".join(problems)
            )
        # frozen dataclass 仍可在 __post_init__ 里写 object.__setattr__ 建立索引；
        # 这里选择不建：边表规模是个位数，线性查找比多一份可变缓存更不容易错。
        # 唯一性已由构造校验保证，(source, target) 至多一条边。

    # ------------------------------------------------------------------ #
    # 结构查询
    # ------------------------------------------------------------------ #
    def allowed_transitions(self) -> dict[StatusT, frozenset[StatusT]]:
        """邻接集投影：``{source: {允许的 target}}``，按 ``states`` 声明序。

        契约闸门（``scripts/contract_check.py::_STATE_MACHINE_SOURCES``）按这个
        结构比对 ``state_machines.yaml``。派生方向唯一：边表 → 投影，不存在第二份
        真相，也就不存在「改了一处忘了另一处」。
        """
        return {
            state: frozenset(
                edge.target for edge in self.edges if edge.source == state
            )
            for state in self.states
        }

    def allowed_targets(self, source: StatusT) -> frozenset[StatusT]:
        """从 ``source`` 出发的允许目标集合。"""
        return frozenset(edge.target for edge in self.edges if edge.source == source)

    def edges_from(self, source: StatusT) -> tuple[Transition[StatusT], ...]:
        """从 ``source`` 出发的边，保持声明顺序（审批链逐跳推进依赖顺序）。"""
        return tuple(edge for edge in self.edges if edge.source == source)

    def is_terminal(self, source: StatusT) -> bool:
        """是否为终态：没有任何出边。

        终态的语义由调用方决定（本项目统一为「不可原地复活」），本模块只回答
        「有没有出边」这一结构事实。
        """
        return not self.allowed_targets(source)

    def terminal_states(self) -> tuple[StatusT, ...]:
        """全部终态，按 ``states`` 声明序。"""
        return tuple(state for state in self.states if self.is_terminal(state))

    # ------------------------------------------------------------------ #
    # 子集合冻结：父对象处于某状态时，是否允许新增子对象
    # ------------------------------------------------------------------ #
    def is_frozen(self, status: StatusT) -> bool:
        """该状态下是否禁止新增子对象。

        所有「在父对象下创建子对象」的路径都从这里派生，而不是各自写 if——
        这是本模块存在 ``frozen_states`` 的唯一理由。
        """
        return status in self.frozen_states

    def assert_child_creation_allowed(
        self, status: StatusT, *, child_object: str
    ) -> None:
        """父对象冻结时拒绝新增子对象。

        **调用方必须在持有父行锁之后调用**：本函数只回答「此刻读到的状态是否冻结」，
        它无法阻止「读的时候还没归档」。并发正确性由锁保证（见 ADR-R27），
        不由本函数保证——把这两件事混在一起，就会得到一个「看起来有检查、
        实际上有窗口」的实现。
        """
        if not self.is_frozen(status):
            return
        frozen = "、".join(_status_value(state) for state in self.frozen_states)
        raise DomainError(
            ErrorRegistry.PARENT_LIFECYCLE_FROZEN,
            detail=(
                f"{self.object_name} 当前状态 {_status_value(status)} 属冻结态"
                f"（{frozen}），不得新增 {child_object}"
            ),
        )

    def frozen_labels(self) -> tuple[str, ...]:
        """冻结态的可读标签（报告与错误信息用）。"""
        return tuple(_status_value(state) for state in self.frozen_states)

    # ------------------------------------------------------------------ #
    # 判定（结构 + 所有权）
    # ------------------------------------------------------------------ #
    def edge_for(
        self, source: StatusT, target: StatusT
    ) -> Transition[StatusT] | None:
        """取指定边；未登记返回 None（由调用方按「不可达」处理）。"""
        for edge in self.edges:
            if edge.source == source and edge.target == target:
                return edge
        return None

    def assert_registered(
        self, source: StatusT, target: StatusT
    ) -> Transition[StatusT]:
        """未登记转移一律拒绝（31.5），返回该边以便调用方复用。"""
        edge = self.edge_for(source, target)
        if edge is None:
            raise DomainError(
                ErrorRegistry.STATE_TRANSITION_DENIED,
                detail=(
                    f"{self.object_name} 非法状态转移："
                    f"{_status_value(source)} -> {_status_value(target)}"
                    "（未登记，已拒绝）"
                ),
            )
        return edge

    def edge_owned_by(
        self, source: StatusT, target: StatusT, action: str
    ) -> Transition[StatusT]:
        """取边并校验所有权：不属于该动作的边，对该动作**不可达**。

        这是 N-07 的结构性修法。顺序是刻意的——先判结构（未登记即拒），再判
        所有权，因此「不存在的边」与「存在但不归你」得到两种可区分的错误，
        而两者都发生在任何业务判断之前。
        """
        edge = self.assert_registered(source, target)
        if edge.owner_action != action:
            raise DomainError(
                ErrorRegistry.STATE_TRANSITION_DENIED,
                detail=(
                    f"{self.object_name} 转移 {edge.label} 由 "
                    f"{edge.owner_action} 独占，{action} 不得推进该边"
                ),
            )
        return edge

    def next_edge_owned_by(
        self, source: StatusT, action: str
    ) -> Transition[StatusT] | None:
        """从当前状态出发、由指定动作拥有的下一条边（审批链逐跳推进用）。

        返回 None 有两种语义：该状态没有出边（终态），或出边都不归该动作。
        调用方通常把两者合并为「当前状态不可由本动作推进」，这是正确的——
        对外不需要区分「走完了」与「轮不到你」。
        """
        for edge in self.edges:
            if edge.source == source and edge.owner_action == action:
                return edge
        return None


def validate_lifecycle[StatusT](
    object_name: str,
    states: Iterable[StatusT],
    edges: Iterable[Transition[StatusT]],
    frozen_states: Iterable[StatusT] = (),
) -> list[str]:
    """边表与冻结态的结构自洽校验（纯函数，供负向测试喂构造数据）。

    返回问题列表，空表示合法。``Lifecycle.__post_init__`` 在非空时抛错——
    把判定与「抛不抛」分开，才能测「重复边真的会被拒」，而不是只能测
    「合法边表能构造成功」（后者对错误实现同样成立）。

    ``frozen_states`` 的两条校验不是形式主义：
    - **必须 ⊆ states**：写了一个不存在的状态（例如把枚举误写成同名字符串）时，
      ``status in frozen_states`` 永远为假，冻结**静默失效**，且没有任何运行期
      报错会提醒。这是本项目最忌讳的失败形态，必须在 import 期炸。
    - **不得重复**：重复项不影响 ``in`` 判定，但会让错误信息与报告里的冻结态清单
      出现重复字面量，掩盖「到底声明了几条」。
    """
    state_list = list(states)
    edge_list = list(edges)
    frozen_list = list(frozen_states)
    problems: list[str] = []

    if not state_list:
        problems.append("states 为空：邻接集投影会缺全部行")
    else:
        duplicates = _duplicates(state_list)
        if duplicates:
            problems.append(f"states 有重复项：{duplicates}")

    if not edge_list:
        problems.append("edges 为空：该对象不可能发生任何转移")

    known = set(state_list)
    for edge in edge_list:
        if edge.source not in known:
            problems.append(
                f"边 {edge.label} 的 source 不在 states 内"
                f"（{_status_value(edge.source)}）"
            )
        if edge.target not in known:
            problems.append(
                f"边 {edge.label} 的 target 不在 states 内"
                f"（{_status_value(edge.target)}）"
            )
        if not edge.owner_action.strip():
            problems.append(f"边 {edge.label} 的 owner_action 为空")

    frozen_duplicates = _duplicates(frozen_list)
    if frozen_duplicates:
        problems.append(f"frozen_states 有重复项：{frozen_duplicates}")
    for state in frozen_list:
        if state not in known:
            problems.append(
                f"frozen_states 含未声明状态 {_status_value(state)!r}"
                "：in 判定将永远为假，冻结会静默失效"
            )

    seen: dict[tuple[object, object], str] = {}
    for edge in edge_list:
        key = (edge.source, edge.target)
        if key in seen:
            problems.append(
                f"边 {edge.label} 重复登记"
                f"（owners={seen[key]} / {edge.owner_action}）："
                "同一条边只能有一个所有者，否则准入判定取决于遍历顺序"
            )
            continue
        seen[key] = edge.owner_action

    return problems


def _duplicates(items: Sequence[object]) -> list[str]:
    """返回重复出现过的元素（按首次出现序，转成字符串便于报错）。"""
    seen: set[object] = set()
    result: list[str] = []
    for item in items:
        if item in seen:
            result.append(_status_value(item))
            continue
        seen.add(item)
    return result


def _status_value(status: object) -> str:
    """状态值统一取 ``.value``；已是字符串时原样返回。

    与 ``status_transition._status_value`` 同口径：序列化形态不应由各处自行决定。
    """
    value = getattr(status, "value", status)
    return str(value)


__all__ = [
    "Lifecycle",
    "Transition",
    "validate_lifecycle",
]
