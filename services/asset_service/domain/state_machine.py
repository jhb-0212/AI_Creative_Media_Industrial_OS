"""资产域状态机（单一事实源，对齐 contracts/state_machines/state_machines.yaml）。

契约（state_machines.yaml: AssetVersion）：
    states: GENERATED→TECHNICAL_QC→HUMAN_REVIEW→APPROVED→GOLD；
            可进入 REJECTED / QUARANTINED / REWORK / SUPERSEDED / ARCHIVED

实现范围：仅登记契约中**有明确来源边**的四条转移。旁支状态（REJECTED 等）
在正文只写「可进入」，未写来源状态；按 31.5 不猜测来源边，待契约补登
（ADR-R08）。因此 GOLD 在本实现中为终态（无出边）。

本模块的两种结构变化（按发生顺序）
----------------------------------
**第二轮审查 P1-02**：边带所有者与准入守卫。旧的邻接集只回答「能不能走」，
不回答「够不够格走」；证据门槛与职责分离被写进两个用例方法，各持一份，必然
漂移——实测 ``change_status`` 只给 GOLD 加门槛，``APPROVED`` 没有，通用接口
可绕过审批。

**N-07（D5）**：边表形态统一到共享层 ``packages/shared/lifecycle.py``。本域此前
就已是「边表 + 所有权 + 守卫」，但用的是本模块私有的一份 ``Transition`` 定义；
项目域则是邻接集。两份形态意味着「所有权是否被执行」取决于你在哪个域——同一
条不变量两种落实方式，就是一种会被漏掉的方式。现在只有一种形态，且聚合根的
``transition_to`` 自己执行所有权判定（不再依赖服务层「记得先查」）。

兼容性：``ASSET_ALLOWED_TRANSITIONS`` 仍由本模块提供，改为**从边表派生**的
投影（契约闸门按它比对 YAML 的结构）。派生方向唯一，不存在两份真相。

设计要点：
- 纯声明式数据 + 校验函数，无副作用、无框架依赖。
- 转移表集中在本模块，contract_check 可反射比对契约，避免分散到聚合根内部。
"""

from __future__ import annotations

from packages.shared.lifecycle import Lifecycle, Transition
from services.asset_service.domain.enums import AssetStatus
from services.asset_service.domain.guards import (
    QC_EVIDENCE,
    RIGHTS_EVIDENCE,
    SEPARATION,
)

# 拥有「审批链」两条边的动作（TECHNICAL_QC → HUMAN_REVIEW → APPROVED）。
APPROVE_ACTION = "ASSET.VERSION.APPROVE"
STATUS_CHANGE_ACTION = "ASSET.STATUS.CHANGE"
QC_REPORT_ACTION = "QC.REPORT.CREATE"

# 唯一权威边表：结构、所有者、准入条件都只在这里定义一次。
ASSET_TRANSITIONS: tuple[Transition[AssetStatus], ...] = (
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

ASSET_LIFECYCLE = Lifecycle(
    object_name="AssetVersion",
    states=tuple(AssetStatus),
    edges=ASSET_TRANSITIONS,
)

# 结构投影：契约闸门按「邻接集」比对 YAML。派生自边表，方向唯一。
ASSET_ALLOWED_TRANSITIONS: dict[AssetStatus, frozenset[AssetStatus]] = (
    ASSET_LIFECYCLE.allowed_transitions()
)


def can_transition_asset(source: AssetStatus, target: AssetStatus) -> bool:
    """判断 AssetVersion 的 source -> target 是否为已登记转移。"""
    return ASSET_LIFECYCLE.edge_for(source, target) is not None


def assert_transition_asset(source: AssetStatus, target: AssetStatus) -> Transition[AssetStatus]:
    """校验转移结构合法性，未登记则拒绝（31.5）。不含准入守卫。"""
    return ASSET_LIFECYCLE.assert_registered(source, target)


def edge_for(source: AssetStatus, target: AssetStatus) -> Transition[AssetStatus] | None:
    """取指定的边（未登记返回 None，由调用方按「不可达」处理）。"""
    return ASSET_LIFECYCLE.edge_for(source, target)


def edge_owned_by(source: AssetStatus, target: AssetStatus, action: str) -> Transition[AssetStatus]:
    """取边并校验所有权：不属于该动作的边，对该动作不可达。

    这是 P1-02 的结构性修法：``change_status`` 请求 ``APPROVED`` 时不看证据、
    也不看角色，因为在它之前这里就已经判定「这条边不归你」。

    服务层保留这次显式调用（而非只依赖聚合根的 ``transition_to``）是因为它
    需要**边对象**来取 ``guards`` 并一次取齐外部事实：先取边、再求值守卫、
    最后推进，三步用同一个边对象，不存在「拿 A 的守卫、走 B 的边」的窗口。
    """
    return ASSET_LIFECYCLE.edge_owned_by(source, target, action)


def next_edge_owned_by(source: AssetStatus, action: str) -> Transition[AssetStatus] | None:
    """从当前状态出发、由指定动作拥有的下一条边（审批链逐跳推进用）。"""
    return ASSET_LIFECYCLE.next_edge_owned_by(source, action)


__all__ = [
    "APPROVE_ACTION",
    "ASSET_ALLOWED_TRANSITIONS",
    "ASSET_LIFECYCLE",
    "ASSET_TRANSITIONS",
    "QC_REPORT_ACTION",
    "STATUS_CHANGE_ACTION",
    "AssetStatus",
    "assert_transition_asset",
    "can_transition_asset",
    "edge_for",
    "edge_owned_by",
    "next_edge_owned_by",
]
