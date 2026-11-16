"""通用审批引擎的取值集合。

为什么单独成文件：本域的枚举必须和 `services/*/domain/enums.py` 一样接受
`check_enum_parity` 的登记检查——新增枚举不登记即报红。三者都在此文件中，
稍后通过 `contracts/runtime/enum_bindings.yaml` 逐一登记或写明豁免理由。
"""

from __future__ import annotations

from enum import StrEnum


class ApprovalMode(StrEnum):
    """多主体审批的组合语义。

    ALL = 会签（每个分组都要同意），ANY = 或签（任一分组同意即可）。
    授权层对含 `+` 的动作仍一律拒绝（不得把 AND 读成 OR）；多主体语义
    只由这里的模式 + 票据分组表达，见 ADR-R11-G 第 2.2 节。
    """

    ALL = "ALL"
    ANY = "ANY"


class TicketStatus(StrEnum):
    """审批票据的状态机。

    CANCELLED 可表示但当前不可进入（没有撤销审批的动作契约），
    由 `state_machine_gaps.yaml` 显式登记，不靠枚举缺值隐式表达。
    """

    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


class VoteDecision(StrEnum):
    """单票结论（投票事实一经写入不可改）。"""

    APPROVE = "APPROVE"
    REJECT = "REJECT"
