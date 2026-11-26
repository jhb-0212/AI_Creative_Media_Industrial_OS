"""维护工单的状态与类型：**唯一**的迁移/领域/测试共用事实源。

为什么状态机放在 shared 而不是只写在迁移里
------------------------------------------
工单状态机有两类消费者：数据库（触发器，负责**真的**拒绝非法跳转）与应用层
（创建/审批时给出可读错误）。两边各写一份白名单，迟早会对「PROPOSED 能不能直接
跳到 SUCCEEDED」给出不同答案，而差异只在生产的一次误操作里暴露。

所以允许的边在这里声明一次，迁移用它们**生成**触发器（而不是另抄一遍 SQL），
测试用它们枚举全部合法/非法跳转。

状态语义
--------
``REQUESTED``  已提交，未审批；只能被批准或取消。
``APPROVED``   已授权执行；只能开跑或取消。
``RUNNING``    执行中；只能成功或失败。
``SUCCEEDED`` / ``FAILED`` / ``CANCELLED``  终态，无出边。

「取消」只允许在**尚未开跑**前发生：一旦 RUNNING，取消等于「结果未知」，
而结果未知必须显式记为 FAILED，否则工单会停在一个人人都以为没执行的状态。
"""

from __future__ import annotations

WORK_ORDER_STATES: tuple[str, ...] = (
    "REQUESTED",
    "APPROVED",
    "RUNNING",
    "SUCCEEDED",
    "FAILED",
    "CANCELLED",
)

WORK_ORDER_STATE_SET: frozenset[str] = frozenset(WORK_ORDER_STATES)

# 键 = 起点，值 = 允许的终点（集合语义：同一对边只声明一次）。
WORK_ORDER_TRANSITIONS: dict[str, frozenset[str]] = {
    "REQUESTED": frozenset({"APPROVED", "CANCELLED"}),
    "APPROVED": frozenset({"RUNNING", "CANCELLED"}),
    "RUNNING": frozenset({"SUCCEEDED", "FAILED"}),
    "SUCCEEDED": frozenset(),
    "FAILED": frozenset(),
    "CANCELLED": frozenset(),
}

# 工单类型：三种**独立**命令，不合并成一个万能操作。全域检测只读、逐租户对账
# 只读，修复才写——把三者合成一条命令会让「只读」的那一半也拿到写权限。
WORK_ORDER_KINDS: tuple[str, ...] = ("DETECT", "RECONCILE", "REPAIR")

TERMINAL_STATES: frozenset[str] = frozenset(
    {"SUCCEEDED", "FAILED", "CANCELLED"}
)


def transition_edges() -> tuple[tuple[str, str], ...]:
    """把转移表摊平成 ``(from, to)`` 序列（触发器 VALUES 与测试都从它派生）。"""
    return tuple(
        (source, target)
        for source in WORK_ORDER_STATES
        for target in sorted(WORK_ORDER_TRANSITIONS[source])
    )


def is_legal_transition(source: str, target: str) -> bool:
    return target in WORK_ORDER_TRANSITIONS.get(source, frozenset())


__all__ = [
    "TERMINAL_STATES",
    "WORK_ORDER_KINDS",
    "WORK_ORDER_STATES",
    "WORK_ORDER_STATE_SET",
    "WORK_ORDER_TRANSITIONS",
    "is_legal_transition",
    "transition_edges",
]
