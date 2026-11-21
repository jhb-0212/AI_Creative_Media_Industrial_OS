"""`Lifecycle.frozen_states` 的负向与正向测试。

为什么要单独一个文件
--------------------
「父对象冻结时不得新增子对象」这条不变量，失效方式是**静默**的：状态写错了、
判断挪了位置、或者干脆忘了声明，都不会有异常，只会让某条创建路径继续放行。
所以这里的每个用例都成对出现——既要证明「冻结时拒绝」，也要证明「非冻结时放行」，
只测一侧等于给一个恒拒绝的实现背书。

覆盖的四种失效形态：
1. 声明里写了不存在的状态 → 冻结静默失效（`status in frozen_states` 恒假）；
2. 声明重复 → 报告里的冻结态清单不可信；
3. 冻结态与终态混淆 → 把 `COMPLETED`（非终态）排除在冻结集合外时无人发现；
4. 判断放在锁外 → 属并发问题，本文件测不到，由 ADR-R27 的锁设计覆盖（不在此假装测过）。
"""

from __future__ import annotations

import pytest

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.shared.lifecycle import Lifecycle, Transition, validate_lifecycle
from services.project_planning.domain.enums import EpisodeStatus, ProjectStatus
from services.project_planning.domain.state_machine import (
    EPISODE_LIFECYCLE,
    EPISODE_STATUS_CHANGE_ACTION,
    PROJECT_LIFECYCLE,
    PROJECT_STATUS_CHANGE_ACTION,
)

_ACTION = "TEST.STATUS.CHANGE"


def _episode_lifecycle(frozen: tuple[EpisodeStatus, ...]) -> Lifecycle[EpisodeStatus]:
    """构造一个三态最小边表，用于把 frozen_states 单独拎出来测。"""
    return Lifecycle(
        object_name="Probe",
        states=tuple(EpisodeStatus),
        edges=(
            Transition(EpisodeStatus.DRAFT, EpisodeStatus.ACTIVE, _ACTION),
            Transition(EpisodeStatus.ACTIVE, EpisodeStatus.ARCHIVED, _ACTION),
        ),
        frozen_states=frozen,
    )


# --------------------------------------------------------------------------- #
# 构造期校验：错误的声明必须在 import 期炸，不能留到运行期静默失效
# --------------------------------------------------------------------------- #
def test_frozen_state_must_exist_in_states() -> None:
    """拼错状态名时必须抛错——否则 `in` 恒假，冻结静默失效。"""
    problems = validate_lifecycle(
        "Probe",
        tuple(EpisodeStatus),
        (Transition(EpisodeStatus.DRAFT, EpisodeStatus.ACTIVE, _ACTION),),
        frozen_states=("ARCHIVEDD",),  # type: ignore[arg-type]  # 拼错
    )
    assert any("未声明状态" in p for p in problems), problems


def test_str_enum_tolerates_bare_string_but_that_is_not_a_guarantee() -> None:
    """`_Status` 是 `str` 混入枚举，故裸字符串与枚举相等，**不会**失效。

    这个事实值得钉住，理由有二：
    - 它说明「写字符串」在当前所有 `str, Enum` 状态类型上是安全的，不必为此加
      类型转换（加了反而会掩盖真正的拼写错误）；
    - 但它**不是**可以省略校验的理由：校验防的是拼写错误，而不是类型宽松。
      一旦某个域改用纯 `Enum`（非 str 混入），裸字符串立刻退化成静默失效，
      而那时校验已经在位，会直接炸出来。
    """
    assert EpisodeStatus.ARCHIVED == "ARCHIVED"
    lc = validate_lifecycle(
        "Probe",
        tuple(EpisodeStatus),
        (Transition(EpisodeStatus.DRAFT, EpisodeStatus.ACTIVE, _ACTION),),
        frozen_states=("ARCHIVED",),  # type: ignore[arg-type]
    )
    assert lc == [], lc


def test_frozen_state_duplicate_rejected() -> None:
    problems = validate_lifecycle(
        "Probe",
        tuple(EpisodeStatus),
        (Transition(EpisodeStatus.DRAFT, EpisodeStatus.ACTIVE, _ACTION),),
        frozen_states=(EpisodeStatus.ARCHIVED, EpisodeStatus.ARCHIVED),
    )
    assert any("frozen_states 有重复项" in p for p in problems), problems


def test_constructor_raises_on_invalid_frozen_declaration() -> None:
    """`Lifecycle.__post_init__` 必须把它变成构造失败，而不是返回一个问题列表。"""
    with pytest.raises(ValueError, match="frozen_states"):
        Lifecycle(
            object_name="Probe",
            states=tuple(EpisodeStatus),
            edges=(Transition(EpisodeStatus.DRAFT, EpisodeStatus.ACTIVE, _ACTION),),
            frozen_states=("NOT_A_STATE",),  # type: ignore[arg-type]
        )


def test_empty_frozen_states_is_legal() -> None:
    """不声明是合法的（剧本/资产没有子对象），不得被误报为错误。"""
    lc = _episode_lifecycle(())
    assert lc.frozen_states == ()
    assert lc.frozen_labels() == ()


# --------------------------------------------------------------------------- #
# 判定：两侧各证伪一次
# --------------------------------------------------------------------------- #
def test_frozen_state_rejects_child_creation() -> None:
    lc = _episode_lifecycle((EpisodeStatus.ARCHIVED,))
    assert lc.is_frozen(EpisodeStatus.ARCHIVED) is True
    with pytest.raises(DomainError) as exc:
        lc.assert_child_creation_allowed(EpisodeStatus.ARCHIVED, child_object="Scene")
    assert exc.value.error_code.code == ErrorRegistry.PARENT_LIFECYCLE_FROZEN.code
    assert "Scene" in exc.value.detail


def test_non_frozen_state_allows_child_creation() -> None:
    """反向证伪：若这里也抛，说明实现是「恒拒绝」，上一条的通过毫无意义。"""
    lc = _episode_lifecycle((EpisodeStatus.ARCHIVED,))
    for status in (EpisodeStatus.DRAFT, EpisodeStatus.ACTIVE):
        assert lc.is_frozen(status) is False
        lc.assert_child_creation_allowed(status, child_object="Scene")  # 不抛即通过


def test_frozen_is_not_terminal_alias() -> None:
    """冻结态与终态是交叉关系，不能互相推导。

    `Project.COMPLETED` 不是终态（仍有 COMPLETED->ARCHIVED 边），但可以声明为冻结。
    若实现把 frozen_states 默认取 terminal_states()，本用例会在构造时就不成立——
    这正是要防的「新增状态时静默继承一个没人想过的语义」。
    """
    completed_is_terminal = PROJECT_LIFECYCLE.is_terminal(ProjectStatus.COMPLETED)
    assert completed_is_terminal is False

    probe_frozen = _episode_lifecycle((EpisodeStatus.ACTIVE,))  # ACTIVE 非终态
    assert probe_frozen.is_frozen(EpisodeStatus.ACTIVE) is True
    assert probe_frozen.is_terminal(EpisodeStatus.ACTIVE) is False


# --------------------------------------------------------------------------- #
# 真实声明：把「现在冻结了什么」钉住，防止无声改动
# --------------------------------------------------------------------------- #
def test_episode_archived_freezes_children() -> None:
    assert EPISODE_LIFECYCLE.frozen_labels() == ("ARCHIVED",)
    with pytest.raises(DomainError):
        EPISODE_LIFECYCLE.assert_child_creation_allowed(
            EpisodeStatus.ARCHIVED, child_object="Scene"
        )


def test_project_archived_freezes_children() -> None:
    assert PROJECT_LIFECYCLE.frozen_labels() == ("ARCHIVED",)
    with pytest.raises(DomainError):
        PROJECT_LIFECYCLE.assert_child_creation_allowed(
            ProjectStatus.ARCHIVED, child_object="Episode"
        )


def test_project_completed_currently_not_frozen() -> None:
    """当前按「只冻结 ARCHIVED」落地。本用例是**待裁定的显式化**：

    断言写在这里，是为了让「COMPLETED 也会冻结子集合」这个决定一旦做出时，
    必须同步改这条用例——而不是悄悄改变行为、没有任何测试变红。
    """
    assert PROJECT_LIFECYCLE.is_frozen(ProjectStatus.COMPLETED) is False
    PROJECT_LIFECYCLE.assert_child_creation_allowed(
        ProjectStatus.COMPLETED, child_object="Episode"
    )


def test_episode_actions_are_the_declared_owner() -> None:
    """冻结判定不能顺手改掉边所有权（两者在同一个 Lifecycle 上，容易串味）。"""
    edge = EPISODE_LIFECYCLE.edge_owned_by(
        EpisodeStatus.DRAFT, EpisodeStatus.ACTIVE, EPISODE_STATUS_CHANGE_ACTION
    )
    assert edge.owner_action == EPISODE_STATUS_CHANGE_ACTION
    assert PROJECT_LIFECYCLE.edge_owned_by(
        ProjectStatus.ACTIVE, ProjectStatus.COMPLETED, PROJECT_STATUS_CHANGE_ACTION
    ).owner_action == PROJECT_STATUS_CHANGE_ACTION
