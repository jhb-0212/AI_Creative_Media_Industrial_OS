"""资产迁移边的准入守卫（声明式，纯函数求值）。

为什么守卫必须挂在「边」上，而不是写在用例方法里
------------------------------------------------------------
整改前，证据门槛（权利清单、QC 报告）和职责分离被写在两个用例方法内部：
``approve()`` 里写了一份，``change_status()`` 里对 GOLD 又写了一份。
两份规则必然漂移——实测结果就是 ``change_status`` 只给 GOLD 加了证据门槛，
``APPROVED`` 那条路没有，于是通用状态接口可以绕过专用审批（第二轮审查 P1-02）。

给 ``change_status`` 补一句「target == APPROVED 也要校验证据」只能让当前用例过，
下一个新增旁支状态（REJECTED / REWORK / SUPERSEDED）还会再漏一次。真正的修法是
把准入条件变成**转移边的属性**：边是唯一权威，任何推进路径都必须带着这条边的
守卫一起走，没有第二条路可以绕。

守卫的职责边界：
- 只做判断，不取数。需要外部证据的守卫用 ``needs_qc_count`` 声明，由调用方
  一次取齐（一条 SQL），守卫本身不查库——否则每个守卫都要自带 IO，不可单测。
- 只抛 ``DomainError``，不返回布尔值：缺证据必须给出「缺的是哪一种」，
  统一错误码 ``STATE_TRANSITION_DENIED``，两条路径返回同码同语义。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.identity.separation import require_different_subject

if TYPE_CHECKING:  # 仅类型标注，避免与聚合根循环导入
    from services.asset_service.domain.asset_version import AssetVersion


@dataclass(frozen=True)
class GuardContext:
    """守卫求值所需的外部事实（由服务层一次性取齐）。"""

    actor: str
    qc_report_count: int = 0


GuardCheck = Callable[["AssetVersion", GuardContext], None]


@dataclass(frozen=True)
class Guard:
    """一条准入条件：名字 + 是否需 QC 计数 + 判定函数。"""

    name: str
    needs_qc_count: bool
    check: GuardCheck


def _check_rights(asset: AssetVersion, context: GuardContext) -> None:
    """权利证据：没有 rights_manifest_ref 的资产不得进入需授权的状态。"""
    if not asset.rights_manifest_ref:
        raise DomainError(
            ErrorRegistry.STATE_TRANSITION_DENIED,
            detail="缺少 rights_manifest_ref：无权利证据不得推进",
        )


def _check_qc(asset: AssetVersion, context: GuardContext) -> None:
    """QC 证据：至少一份检测报告，否则「审过了」无凭据。"""
    if context.qc_report_count == 0:
        raise DomainError(
            ErrorRegistry.STATE_TRANSITION_DENIED,
            detail="缺少 QC 报告：无检测证据不得推进",
        )


def _check_separation(asset: AssetVersion, context: GuardContext) -> None:
    """职责分离：推进人不得是创建人（禁止自批，Agent 不能自审自批）。"""
    require_different_subject(
        actor=context.actor,
        counterpart=asset.created_by,
        action_label="ASSET 生命周期推进",
        actor_label="推进人",
        counterpart_label="创建人",
    )


RIGHTS_EVIDENCE = Guard("rights_evidence", False, _check_rights)
QC_EVIDENCE = Guard("qc_evidence", True, _check_qc)
SEPARATION = Guard("separation", False, _check_separation)


def required_qc_count(guards: tuple[Guard, ...]) -> bool:
    """这组守卫生否需要 QC 计数（用于决定是否发起一次查库）。"""
    return any(g.needs_qc_count for g in guards)


def evaluate(
    guards: tuple[Guard, ...], asset: AssetVersion, context: GuardContext
) -> None:
    """按声明顺序求值，任一不满足即抛错（fail-closed）。"""
    for guard in guards:
        guard.check(asset, context)
