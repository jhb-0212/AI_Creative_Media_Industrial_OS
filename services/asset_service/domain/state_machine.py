"""资产域状态机（单一事实源，对齐 contracts/state_machines/state_machines.yaml）。

契约（state_machines.yaml: AssetVersion）：
    states: GENERATED→TECHNICAL_QC→HUMAN_REVIEW→APPROVED→GOLD；
            可进入 REJECTED / QUARANTINED / REWORK / SUPERSEDED / ARCHIVED

实现范围：仅登记契约中**有明确来源边**的四条转移。旁支状态（REJECTED 等）
在正文只写「可进入」，未写来源状态；按 31.5 不猜测来源边，待契约补登
（ADR-R08）。因此 GOLD 在本实现中为终态（无出边）。

设计要点：
- 纯声明式数据 + 校验函数，无副作用、无框架依赖。
- 转移表集中在本模块，contract_check 可反射比对契约，避免分散到聚合根内部。
"""

from __future__ import annotations

from packages.errors.error_codes import DomainError, ErrorRegistry
from services.asset_service.domain.enums import AssetStatus

ASSET_ALLOWED_TRANSITIONS: dict[AssetStatus, frozenset[AssetStatus]] = {
    AssetStatus.GENERATED: frozenset({AssetStatus.TECHNICAL_QC}),
    AssetStatus.TECHNICAL_QC: frozenset({AssetStatus.HUMAN_REVIEW}),
    AssetStatus.HUMAN_REVIEW: frozenset({AssetStatus.APPROVED}),
    AssetStatus.APPROVED: frozenset({AssetStatus.GOLD}),
    # GOLD 无出边：契约未登记 GOLD 的任何后续转移（旁支亦未给出）。
    AssetStatus.GOLD: frozenset(),
}


def can_transition_asset(source: AssetStatus, target: AssetStatus) -> bool:
    """判断 AssetVersion 的 source -> target 是否为已登记转移。"""
    return target in ASSET_ALLOWED_TRANSITIONS.get(source, frozenset())


def assert_transition_asset(source: AssetStatus, target: AssetStatus) -> None:
    """校验 AssetVersion 转移合法性，未登记则拒绝（31.5）。"""
    if not can_transition_asset(source, target):
        raise DomainError(
            ErrorRegistry.STATE_TRANSITION_DENIED,
            detail=f"非法状态转移：{source.value} -> {target.value}（未登记，已拒绝）",
        )
