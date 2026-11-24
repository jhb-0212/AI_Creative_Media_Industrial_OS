"""累积式会签的**凑齐判定**纯函数（ADR-R45 §D3-b/D3-c）。

职责边界
--------
本模块只做判定，不做 IO、不写库、不解析 Principal。它是「一组签署事件是否已经
凑齐某个动作的会签」的**唯一**判据，服务层与边界断言都调用它——判据只有一份，
不会出现「服务说凑齐、闸门说没有」的分裂。

为什么判据必须纯函数
--------------------
「跨席位不同主体」「每席位恰好一个有效签署」「消费后整组失效」这三条是安全不变量，
散落在服务层的 if 里，稍有改动就会静默放宽。收敛到一个可穷举单测的纯函数，
才能对每条不变量做负向注入（构造坏样本，断言报红）。

派生，不手写清单
----------------
席位集合从 ``principal_seats(action_key)`` 派生，不在本模块写死任何动作的席位数——
新增/改动动作的席位时判据自动跟随（清单不会自己举手，G-A/G-C 教训）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from packages.shared.approval_signature import SignatureEventKind


@dataclass(frozen=True)
class SignatureView:
    """签署事件的最小判定视图（从任意来源投影，不要求具体类型）。

    ``subject`` 是签署主体标识；``event_kind`` 取 ``SignatureEventKind`` 的值或字符串；
    ``seat_index`` 从 1 开始。
    """

    seat_index: int
    subject: str
    event_kind: str


@dataclass(frozen=True)
class CountersignVerdict:
    """凑齐判定结果（不可变，调用方只读）。

    ``complete`` 为真仅当每个席位恰好一个有效签署、且跨席位主体互不相同、且未被消费。
    ``problems`` 列出为何未凑齐（每条点名具体席位/主体，fail-closed 时可回传给调用方）。
    ``effective`` 是「席位 → 签署主体」的映射（仅 complete 时有意义）。
    """

    complete: bool
    problems: tuple[str, ...]
    effective: dict[int, str]


def _as_kind(value: Any) -> str:
    """把 SignatureEventKind / 字符串归一为大写字串（容错，不吞未知值）。"""
    return value.value if isinstance(value, SignatureEventKind) else str(value).upper()


def evaluate_countersignature(
    *,
    action_key: str,
    events: tuple[SignatureView, ...],
    seats: tuple[frozenset[str], ...],
) -> CountersignVerdict:
    """判定一组签署事件是否满足 ``action_key`` 的会签要求。

    参数 ``seats`` 由调用方从 ``principal_seats(action_key)`` 传入（保持本模块无
    IO、无对身份服务的 import）。``action_key`` 只用于错误信息点名。

    判据（全部满足才 complete）：
    1. 无该组的 CONSUMED 事件——消费后整组一次性失效，不可重放；
    2. 每个席位恰好一个**有效**签署——有效 = 有 SIGNED，且无同 (seat, subject)
       的更晚 REVOKED；多于一个有效签署视为异常（撤销流程被绕过），报红而非取第一个；
    3. 跨席位主体两两不同——单主体签满所有席位 = 会签被静默降级为单人授权。
    """
    problems: list[str] = []

    # 0) 席位集合为空 = 契约错误（动作未配置 allowed_principals），fail-closed。
    #    不能让它走成 vacuous truth——「没有任何席位要求」若算凑齐，等于把
    #    会签要求静默抹掉，正是本判据要防的第一号失败形态。
    if not seats:
        return CountersignVerdict(
            complete=False,
            problems=(f"{action_key}: 会签席位集合为空（契约错误，fail-closed）",),
            effective={},
        )

    # 1) 消费检查：只要有 CONSUMED，整组作废。
    if any(_as_kind(e.event_kind) == SignatureEventKind.CONSUMED.value for e in events):
        return CountersignVerdict(
            complete=False,
            problems=(f"{action_key}: 签署组已被消费（CONSUMED），不可重放",),
            effective={},
        )

    # 2) 派生每席位有效签署：按 (seat, subject) 分组，看最后一次事件。
    #    append-only 事件日志里，同 (seat, subject) 的 REVOKED 是撤回，SIGNED 是签署。
    last: dict[tuple[int, str], str] = {}
    for e in events:
        kind = _as_kind(e.event_kind)
        if kind not in (SignatureEventKind.SIGNED.value, SignatureEventKind.REVOKED.value):
            continue
        key = (e.seat_index, e.subject)
        # 同键后写覆盖先写（事件按 occurred_at 顺序传入；重复 SIGN 由 DB 唯一约束挡）。
        last[key] = kind
    effective: dict[int, str] = {}
    for (seat, subject), kind in last.items():
        if kind == SignatureEventKind.SIGNED.value:
            if seat in effective:
                problems.append(
                    f"{action_key}: 席位 {seat} 有超过一个有效签署（{effective[seat]} 与 {subject}）"
                )
            else:
                effective[seat] = subject

    # 3) 每席位恰好一个有效签署。
    for index in range(1, len(seats) + 1):
        if index not in effective:
            problems.append(f"{action_key}: 席位 {index} 尚无有效签署")

    # 4) 跨席位主体互不相同（会签不得降级为单人授权）。
    subjects = list(effective.values())
    if len(subjects) != len(set(subjects)):
        seen: dict[str, int] = {}
        for seat, subject in effective.items():
            if subject in seen:
                problems.append(
                    f"{action_key}: 主体 {subject} 同时签署席位 {seen[subject]} 与 {seat}，"
                    "会签被降级为单人授权"
                )
            else:
                seen[subject] = seat

    # 席位范围外的签署（action 席位变更后残留的旧签署）——指明但不_complete。
    for seat in effective:
        if seat < 1 or seat > len(seats):
            problems.append(f"{action_key}: 存在越界席位 {seat} 的签署")

    return CountersignVerdict(
        complete=not problems,
        problems=tuple(problems),
        effective=effective if not problems else {},
    )


__all__: list[str] = [
    "CountersignVerdict",
    "SignatureView",
    "evaluate_countersignature",
]
