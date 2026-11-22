"""预算域的受控枚举（非生命周期维度，手工维护）。

为什么 transaction_kind 需要一份枚举而不是约定字符串
---------------------------------------------------
``BudgetLedger`` 的 ``transaction_kind`` 不是 ``status``：它不描述「这条流水处在哪个
阶段」，而描述「这条流水在账上做什么动作」。但它同样是**不能写错**的维度——
把 ``"RELEASE"`` 误敲成 ``"RELEASED"`` 会得到一条谁都解释不了的流水，余额计算口径
当场分裂，而 ``status`` 的三层校验完全看不见它（status 是 NO_LIFECYCLE 哨兵）。

所以它是独立的受控维度：代码里一个枚举、库里一条 CHECK、契约里一份 enum 声明，
三处都从这同一个集合派生。新增取值必须同时改三处，否则迁移会在库层拒绝写入。

RESERVE→RELEASE 是「释放未消耗的预留」，RESERVE→REFUND 是「对已结算额度做冲销」；
两者都以 ``reservation_ref`` 指回被处理的那条 RESERVE 流水，因此「释放两次」在
库层被唯一索引拒绝，而不是靠服务层「记得查一下」。
"""

from __future__ import annotations

from enum import Enum


class BudgetTransactionKind(str, Enum):
    """账本流水的动作类型（受控；写入即不可变）。"""

    RESERVE = "RESERVE"
    RELEASE = "RELEASE"
    REFUND = "REFUND"


# 需要指回预留言本的种类：这两类没有 reservation_ref 就无从判断「释放的是哪一笔」。
SETTLEMENT_KINDS: frozenset[BudgetTransactionKind] = frozenset(
    {BudgetTransactionKind.RELEASE, BudgetTransactionKind.REFUND}
)


def transaction_kind_values() -> list[str]:
    """给迁移与 ORM 用的取值列表（字母序，便于逐字节比对）。"""
    return sorted(kind.value for kind in BudgetTransactionKind)


__all__ = ["SETTLEMENT_KINDS", "BudgetTransactionKind", "transaction_kind_values"]
