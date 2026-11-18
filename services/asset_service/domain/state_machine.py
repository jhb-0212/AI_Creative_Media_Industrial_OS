"""资产域状态机（单一事实源，对齐 contracts/state_machines/state_machines.yaml）。

契约（state_machines.yaml: AssetVersion）：
    states: GENERATED→TECHNICAL_QC→HUMAN_REVIEW→APPROVED→GOLD；
            可进入 REJECTED / QUARANTINED / REWORK / SUPERSEDED / ARCHIVED

实现范围：仅登记契约中**有明确来源边**的四条转移。旁支状态（REJECTED 等）
在正文只写「可进入」，未写来源状态；按 31.5 不猜测来源边，待契约补登
（ADR-R08）。因此 GOLD 在本实现中为终态（无出边）。

本轮结构性变化（第二轮审查 P1-02 根治）：**边带所有者与准入守卫**
------------------------------------------------------------
旧的转移表只是一个邻接集（``{source: {target}}``），只回答「能不能走」，
不回答「够不够格走」。证据门槛与职责分离因此被写进两个用例方法里，
``approve()`` 与 ``change_status()`` 各持一份，必然漂移——实测结果就是
``change_status`` 只给 GOLD 加门槛，``APPROVED`` 没有，通用接口可绕过审批。

现在每条边声明两件事：
- ``owner_action``：唯一被允许走这条边的动作。``APPROVED`` 那条边属于
  ``ASSET.VERSION.APPROVE``，于是通用状态接口的可达集里根本没有它——
  不是靠多加一个 if 拦住，而是那条边不在它的可达集里。
- ``guards``：准入条件（见 domain/guards.py）。任何路径都必须带着这条边的
  守卫走，没有第二套规则可以漂移。

兼容性：``ASSET_ALLOWED_TRANSITIONS`` 仍由本模块提供，但改为**从边表派生**的
投影（契约闸门按它比对 YAML 的结构）。派生方向唯一，不存在两份真相。

设计要点：
- 纯声明式数据 + 校验函数，无副作用、无框架依赖。
- 转移表集中在本模块，contract_check 可反射比对契约，避免分散到聚合根内部。
"""

from __future__ import annotations

from dataclasses import dataclass

from packages.errors.error_codes import DomainError, ErrorRegistry
from services.asset_service.domain.enums import AssetStatus
from services.asset_service.domain.guards import (
    QC_EVIDENCE,
    RIGHTS_EVIDENCE,
    SEPARATION,
    Guard,
)

# 拥有「审批链」两条边的动作（TECHNICAL_QC → HUMAN_REVIEW → APPROVED）。
APPROVE_ACTION = "ASSET.VERSION.APPROVE"
STATUS_CHANGE_ACTION = "ASSET.STATUS.CHANGE"
QC_REPORT_ACTION = "QC.REPORT.CREATE"


@dataclass(frozen=True)
class Transition:
    """一条已登记的转移边：结构 + 所有者 + 准入守卫。"""

    source: AssetStatus
    target: AssetStatus
    owner_action: str
    guards: tuple[Guard, ...] = ()

    @property
    def label(self) -> str:
        return f"{self.source.value} -> {self.target.value}"


# 唯一权威边表：结构、所有者、准入条件都只在这里定义一次。
ASSET_TRANSITIONS: tuple[Transition, ...] = (
    # 登记第一份 QC 报告即把资产推入技术检测态：报告本身就是这条边的准入条件，
    # 因此不再附加证据守卫（否则会出现「要先有报告才能记录报告」的死循环）。
    Transition(
        AssetStatus.GENERATED,
        AssetStatus.TECHNICAL_QC,
        owner_action=QC_REPORT_ACTION,
    ),
    # 审批链第一跳：进入人工复核。证据必须齐备，否则复核无凭据。
    Transition(
        AssetStatus.TECHNICAL_QC,
        AssetStatus.HUMAN_REVIEW,
        owner_action=APPROVE_ACTION,
        guards=(RIGHTS_EVIDENCE, QC_EVIDENCE, SEPARATION),
    ),
    # 审批链第二跳：真正批准。与上一跳同口径，避免两套规则。
    Transition(
        AssetStatus.HUMAN_REVIEW,
        AssetStatus.APPROVED,
        owner_action=APPROVE_ACTION,
        guards=(RIGHTS_EVIDENCE, QC_EVIDENCE, SEPARATION),
    ),
    # 进入受控资产库：契约要求 APPROVED 前置 + 权利 + QC，三者在同一处声明。
    Transition(
        AssetStatus.APPROVED,
        AssetStatus.GOLD,
        owner_action=STATUS_CHANGE_ACTION,
        guards=(RIGHTS_EVIDENCE, QC_EVIDENCE, SEPARATION),
    ),
)

# 结构投影：契约闸门按「邻接集」比对 YAML。派生自边表，方向唯一。
ASSET_ALLOWED_TRANSITIONS: dict[AssetStatus, frozenset[AssetStatus]] = {
    status: frozenset(
        edge.target for edge in ASSET_TRANSITIONS if edge.source is status
    )
    for status in AssetStatus
}

_EDGE_INDEX: dict[tuple[AssetStatus, AssetStatus], Transition] = {
    (edge.source, edge.target): edge for edge in ASSET_TRANSITIONS
}


def can_transition_asset(source: AssetStatus, target: AssetStatus) -> bool:
    """判断 AssetVersion 的 source -> target 是否为已登记转移。"""
    return (source, target) in _EDGE_INDEX


def assert_transition_asset(source: AssetStatus, target: AssetStatus) -> None:
    """校验转移结构合法性，未登记则拒绝（31.5）。不含准入守卫。"""
    if not can_transition_asset(source, target):
        raise DomainError(
            ErrorRegistry.STATE_TRANSITION_DENIED,
            detail=f"非法状态转移：{source.value} -> {target.value}（未登记，已拒绝）",
        )


def edge_for(source: AssetStatus, target: AssetStatus) -> Transition | None:
    """取指定的边（未登记返回 None，由调用方按「不可达」处理）。"""
    return _EDGE_INDEX.get((source, target))


def edge_owned_by(source: AssetStatus, target: AssetStatus, action: str) -> Transition:
    """取边并校验所有权：不属于该动作的边，对该动作不可达。

    这是 P1-02 的结构性修法：``change_status`` 请求 ``APPROVED`` 时不看证据、
    也不看角色，因为在它之前这里就已经判定「这条边不归你」。
    """
    edge = edge_for(source, target)
    if edge is None:
        assert_transition_asset(source, target)  # 抛未登记转移
        raise AssertionError("不可达：assert_transition_asset 未抛错")  # pragma: no cover
    if edge.owner_action != action:
        raise DomainError(
            ErrorRegistry.STATE_TRANSITION_DENIED,
            detail=(
                f"转移 {edge.label} 由 {edge.owner_action} 独占，"
                f"{action} 不得推进该边"
            ),
        )
    return edge


def next_edge_owned_by(source: AssetStatus, action: str) -> Transition | None:
    """从当前状态出发、由指定动作拥有的下一条边（审批链逐跳推进用）。"""
    for edge in ASSET_TRANSITIONS:
        if edge.source is source and edge.owner_action == action:
            return edge
    return None
