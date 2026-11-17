"""平台阶段门禁（Phase Gate）。

**这一层解决什么**

契约把 44 个动作登记为 ``PHASE_GATED（Phase N）``——它们在 Action Registry 与
OpenAPI 里都有明确地址（``/actions/<小写点分 key>``），但在当前平台阶段不提供服务。
没有这一层时，这些调用在运行时表现为 **404**，与"路径拼错"完全不可区分：客户端
无法判断该重试、该升级客户端，还是该改用别的能力；审计里也看不到"有人在敲未开放
的能力"。

``Phase1-实现验收清单`` F1 的要求是"调用 Creator 动作 → ``PHASE_NOT_ENABLED``，
无副作用；**不得以配置开关绕过**"。因此本模块做两件克制的事：

1. 把 ``PHASE_GATED（Phase N）`` 的 status 文本解析成可比较的阶段号；
2. 用**代码常量**声明当前平台阶段。

**为什么阶段是代码常量而不是配置项**

阶段是**发布级事实**：一次发布对应 ``contracts/phases/phase_map.yaml`` 里的一个
阶段。若阶段来自环境变量，等于给了一个 ``AFS_PLATFORM_PHASE=4`` 就能把 Phase 1
变成 Phase 4 的旁路——这正是 F1 明令禁止的"配置开关绕过"。改阶段必须改代码、
过评审、重走发布闸门（``release_package.py`` 会记录 commit），代价与影响面匹配。

**职责边界**

本模块只做**判定**，不装配 HTTP。拒绝面在 ``apps/api/main.py`` 的
``/actions/{action_key}`` 端点（``include_in_schema=False``）。判定与装配分离，
是为了让"当前阶段是什么"这件事可以在单元测试里直接断言，而不必起应用。
"""

from __future__ import annotations

import re
from enum import IntEnum

#: 从 ``PHASE_GATED（Phase 2）`` / ``PHASE_GATED（Phase 2+）`` /
#: ``PHASE_GATED（Phase 3 公开）`` 里取出阶段号。括号后的补充说明（如"公开"、
#: "+"）不参与判定——它描述的是启用条件，不是启用阶段。
_PHASE_RE = re.compile(r"Phase\s*(\d)")


class PlatformPhase(IntEnum):
    """平台阶段（与 ``phase_map.yaml`` 的 Phase 0—4 一一对应）。

    用 ``IntEnum`` 而不是字符串：判定是"是否已达到"，本质是数值比较。
    字符串比较会引入排序语义的误解（``"Phase 10" < "Phase 2"``）。
    """

    PHASE_0 = 0
    PHASE_1 = 1
    PHASE_2 = 2
    PHASE_3 = 3
    PHASE_4 = 4

    @property
    def label(self) -> str:
        """契约与错误上下文里的标准写法（``Phase 1``）。"""
        return f"Phase {int(self)}"


#: 当前平台阶段。**刻意不是配置项**，理由见模块 docstring。
#:
#: 变更流程：改这一行 → 更新 ADR → 重跑全闸门 → 发布（``release_package.py``
#: 把 commit 写进包内元数据，因此"谁把阶段推上去了"是可追溯的）。
PLATFORM_PHASE: PlatformPhase = PlatformPhase.PHASE_1


def parse_required_phase(status: str) -> PlatformPhase | None:
    """从动作的 ``status`` 文本解析所需阶段。

    返回 ``None`` 的两种情况都表示"不是阶段门禁动作"，调用方应据此**不做**
    阶段拒绝（避免把普通动作误判成未启用）：

    - status 不以 ``PHASE_GATED`` 开头（如 ``ACTIVE``）；
    - 以 ``PHASE_GATED`` 开头但括号里没有阶段号。

    第二种是契约书写缺陷。这里不抛异常，因为运行时拒绝一个合法动作的代价
    远高于容忍一条描述性文本——但契约侧有 ``tests/unit/test_phase_gate.py``
    兜底，逐个动作断言 44 条 PHASE_GATED 都能解析出阶段号，写错了测试会红。
    """
    if not status.startswith("PHASE_GATED"):
        return None
    match = _PHASE_RE.search(status)
    if match is None:
        return None
    try:
        return PlatformPhase(int(match.group(1)))
    except ValueError:
        # 契约里出现 Phase 5+ 这种越界写法：不猜，按"非门禁"处理，
        # 由契约检查与单元测试把它暴露出来，而不是在运行期拒绝合法动作。
        return None


def is_blocked(required: PlatformPhase | None, *, current: PlatformPhase) -> bool:
    """``required`` 早于或等于当前阶段时放行（返回 False）。"""
    if required is None:
        return False
    return required > current
