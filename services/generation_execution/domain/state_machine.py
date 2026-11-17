"""generation-execution 域状态机（31.5：未登记转移一律拒绝）。

ProductionRoute：UNVERIFIED→VERIFIED（唯一登记边；VERIFIED 终态）。
- 契约登记：contracts/state_machines/state_machines.yaml → ProductionRoute；
- 数据库层镜像：迁移 a5b6c7d8e9f0 播种同名转移表 + 守卫触发器（ADR-R20），
  直连 SQL 的写入不经服务层，必须在这里拦；
- 一致性由 tests/unit/test_generation_route_parity.py 守（迁移字面量 ↔ 枚举）。
"""

from __future__ import annotations

from packages.errors.error_codes import DomainError, ErrorRegistry
from services.generation_execution.domain.enums import ProductionRouteStatus

PRODUCTION_ROUTE_ALLOWED_TRANSITIONS: dict[
    ProductionRouteStatus, frozenset[ProductionRouteStatus]
] = {
    ProductionRouteStatus.UNVERIFIED: frozenset(
        {ProductionRouteStatus.VERIFIED}
    ),
    # VERIFIED 无出边：验证是对"已提交证据"的记录，撤销验证属新证据问题，
    # 基线未给撤销语义——终态不可原地复活（红线）。
    ProductionRouteStatus.VERIFIED: frozenset(),
}


def can_transition_production_route(
    source: ProductionRouteStatus, target: ProductionRouteStatus
) -> bool:
    """判断 ProductionRoute 的 source -> target 是否为已登记转移。"""
    return target in PRODUCTION_ROUTE_ALLOWED_TRANSITIONS.get(source, frozenset())


def assert_transition_production_route(
    source: ProductionRouteStatus, target: ProductionRouteStatus
) -> None:
    """校验 ProductionRoute 转移合法性，未登记则抛 DomainError。"""
    if not can_transition_production_route(source, target):
        raise DomainError(
            ErrorRegistry.STATE_TRANSITION_DENIED,
            detail=f"非法状态转移：{source.value} -> {target.value}（未登记，已拒绝）",
        )
