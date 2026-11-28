"""会签席位（countersignature seats）解析与授权判定。

背景（AUTH-3 的根因）：``allowed_principals`` 里 ``/`` 与 ``+`` 是两个不同
语义，但旧解析器只按 ``/`` 切分，于是 ``交付负责人+内部 QC/权利职责`` 被解析成
两个「角色」：``交付负责人+内部 QC``（一个永远不存在的角色名）和 ``权利职责``。
后果是有害的安静：判定永远否决，原因却报「缺少角色」，把「这是一条双人
会签要求」伪装成「权限没配好」。

本测试锁定三件事：
1. 席位解析的正确性（``/`` 先结合、``+`` 后组合，括号剥离）；
2. ``authorize`` 对多席位动作**显式拒绝**且错误语义可诊断，不得静默降级成
   单人授权；
3. 契约与解析器保持同步——ACTIVE 动作里凡含 ``+`` 的，必须被识别为多席位
   （否则契约改了而解析器没跟上，又会退回「假角色名」的老路）。
"""

from __future__ import annotations

import uuid

import pytest

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.identity.principal import Principal
from services.identity_access.action_registry import (
    _load_registry,
    parse_allowed_principals,
    parse_principal_seats,
    principal_seats,
)
from services.identity_access.rbac import authorize

_TENANT = uuid.UUID("11111111-2222-3333-4444-555555555555")

# 契约里真实存在的双人动作（正文 31.6 主表：交付负责人+内部 QC/权利职责）。
_COUNTERSIGN_ACTION = "DELIVERY.MANIFEST.APPROVE"


def _principal(roles: set[str], capabilities: set[str] | None = None) -> Principal:
    return Principal(
        subject_id="user-001",
        tenant_id=_TENANT,
        roles=frozenset(roles),
        capabilities=frozenset(capabilities or {"*"}),
    )


# --------------------------------------------------------------------------- #
# 解析：`/` 先结合成候选组，`+` 再把组组合成席位
# --------------------------------------------------------------------------- #
def test_slash_only_is_single_seat() -> None:
    """`版权/法务授权人` 是一个席位、两个候选角色（OR），不是两个席位。"""
    seats = parse_principal_seats("版权/法务授权人")

    assert len(seats) == 1
    assert seats[0] == frozenset({"版权", "法务授权人"})


def test_plus_creates_two_seats_with_slash_grouping_inside() -> None:
    """`交付负责人+内部 QC/权利职责` = 交付负责人 AND (内部 QC OR 权利职责)。

    这条断言固定了运算符优先级：`/` 先结合。若哪天有人把它实现成
    `(交付负责人 AND 内部QC) OR 权利职责`，此处立即报红。
    """
    seats = parse_principal_seats("交付负责人+内部 QC/权利职责")

    assert len(seats) == 2
    assert seats[0] == frozenset({"交付负责人"})
    assert seats[1] == frozenset({"内部 QC", "权利职责"})


def test_parenthetical_qualifier_is_stripped_per_seat() -> None:
    """席位内的资质说明（括号）被剥离，且不影响席位划分。"""
    seats = parse_principal_seats("导演+制片人（授权范围内）/编剧")

    assert seats == (frozenset({"导演"}), frozenset({"制片人", "编剧"}))


def test_empty_text_yields_no_seats() -> None:
    """空配置一律返回空元组（调用方 fail-closed，不得当成「无要求」）。"""
    assert parse_principal_seats("") == ()
    assert parse_principal_seats("   ") == ()


def test_union_projection_keeps_all_roles_for_display() -> None:
    """展示投影仍能列出全部角色，但不再声称它们同属一个席位。"""
    text = "交付负责人+内部 QC/权利职责"

    assert parse_allowed_principals(text) == frozenset({"交付负责人", "内部 QC", "权利职责"})
    # 关键差异：投影里没有「交付负责人+内部 QC」这种假角色名。
    assert not any("+" in role for role in parse_allowed_principals(text))


# --------------------------------------------------------------------------- #
# 判定：多席位动作必须显式拒绝，且不得静默降级成单人授权
# --------------------------------------------------------------------------- #
def test_countersign_action_is_recognized_as_multi_seat() -> None:
    """契约中的双人动作被识别为 2 个席位（若契约改动，此断言会提示复核）。"""
    assert len(principal_seats(_COUNTERSIGN_ACTION)) == 2


def test_countersign_rejects_single_seat_role_with_actionable_message() -> None:
    """持有一个席位的角色也不得放行——会签要求的是「不同主体各签一次」。"""
    principal = _principal({"交付负责人"}, capabilities={"DeliveryManifest:*"})

    with pytest.raises(DomainError) as exc_info:
        authorize(_COUNTERSIGN_ACTION, principal)

    assert exc_info.value.error_code is ErrorRegistry.AUTH_SCOPE_DENIED
    detail = exc_info.value.detail
    # 错误语义必须指向「会签 + 多主体 + 能力未实现」，而不是「缺少角色」。
    assert "会签" in detail
    assert "不同主体" in detail
    assert "不支持多主体累积审批" in detail


def test_countersign_rejects_other_seat_role_too() -> None:
    """第二个席位的任一候选角色（内部 QC / 权利职责）同样不得单独放行。"""
    for role in ("内部 QC", "权利职责"):
        with pytest.raises(DomainError) as exc_info:
            authorize(_COUNTERSIGN_ACTION, _principal({role}))
        assert "会签" in exc_info.value.detail


def test_countersign_without_any_seat_role_reports_missing_role() -> None:
    """连一个席位都担不了时，错误语义应是「缺少角色」而非「会签未实现」。"""
    with pytest.raises(DomainError) as exc_info:
        authorize(_COUNTERSIGN_ACTION, _principal({"无关角色"}))

    assert exc_info.value.error_code is ErrorRegistry.AUTH_SCOPE_DENIED
    assert "缺少执行" in exc_info.value.detail
    assert "会签" not in exc_info.value.detail


def test_single_seat_action_still_allows_matching_role() -> None:
    """单席位动作行为不变：候选角色命中即放行（回归保护）。"""
    principal = _principal({"制片"}, capabilities={"Project:*"})

    assert authorize("PROJECT.CREATE", principal) is principal


def test_contract_plus_actions_are_all_multi_seat() -> None:
    """闸门式一致性：契约里含 `+` 的非门禁动作必须被解析成多席位。

    这条把「契约语义」与「运行时解析」绑在一起。若将来契约新增一条含 `+`
    的动作而解析器退化，此处先报红，而不是等到线上出现「权限怎么配都不通」。
    """
    offenders: list[str] = []
    for key, definition in _load_registry().items():
        if "+" not in definition.allowed_principals:
            continue
        if len(principal_seats(key)) < 2:
            offenders.append(key)

    assert not offenders, f"以下动作含 `+` 却未被识别为会签：{offenders}"
