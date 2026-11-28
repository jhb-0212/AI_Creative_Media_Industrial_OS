"""凑齐判定纯函数的正负向证明（ADR-R45 §D3-b/D3-c，DATA-7）。

判据比实现更容易错：每条不变量都要构造坏样本证明报红，而不是只看「现在正常路径
通过」。ERROR ≠ FAILED——负向注入必须断言具体的 problems 文本，不能用「抛了异常」
代替（夹具构造失败也是抛异常，证明的却是相反的事）。
"""

from __future__ import annotations

from packages.shared.countersign_rules import SignatureView, evaluate_countersignature
from services.identity_access.action_registry import principal_seats

ACTION = "DELIVERY.MANIFEST.APPROVE"
SEATS = principal_seats(ACTION)


def _v(seat: int, subject: str, kind: str = "SIGNED") -> SignatureView:
    return SignatureView(seat_index=seat, subject=subject, event_kind=kind)


def test_seats_derived_from_registry() -> None:
    """席位集合从 Action Registry 派生（2 席位），判据不手写清单。"""
    assert len(SEATS) == 2
    # 第二席位含两种候选角色（内部 QC / 权利职责），第一席位单一角色。
    assert any(len(seat) > 1 for seat in SEATS)


def test_two_distinct_subjects_complete() -> None:
    verdict = evaluate_countersignature(
        action_key=ACTION,
        events=(_v(1, "delivery-lead"), _v(2, "qc-inspector")),
        seats=SEATS,
    )
    assert verdict.complete is True, verdict.problems
    assert verdict.effective == {1: "delivery-lead", 2: "qc-inspector"}


def test_single_subject_signing_all_seats_is_rejected() -> None:
    """会签被静默降级为单人授权 → 必须报红点名主体。"""
    verdict = evaluate_countersignature(
        action_key=ACTION,
        events=(_v(1, "same-person"), _v(2, "same-person")),
        seats=SEATS,
    )
    assert verdict.complete is False
    assert any("same-person" in p and "降级" in p for p in verdict.problems), verdict.problems


def test_missing_seat_is_rejected() -> None:
    verdict = evaluate_countersignature(
        action_key=ACTION, events=(_v(1, "delivery-lead"),), seats=SEATS
    )
    assert verdict.complete is False
    assert any("席位 2" in p and "尚无有效签署" in p for p in verdict.problems)


def test_revoked_signature_is_not_effective() -> None:
    """先签后撤 → 该席位无有效签署。"""
    verdict = evaluate_countersignature(
        action_key=ACTION,
        events=(
            _v(1, "delivery-lead", "SIGNED"),
            _v(1, "delivery-lead", "REVOKED"),
            _v(2, "qc-inspector", "SIGNED"),
        ),
        seats=SEATS,
    )
    assert verdict.complete is False
    assert any("席位 1" in p for p in verdict.problems)


def test_two_valid_signatures_on_one_seat_is_rejected() -> None:
    """同席位两个有效签署 = 撤销流程被绕过，报红而非取第一个。"""
    verdict = evaluate_countersignature(
        action_key=ACTION,
        events=(
            _v(1, "lead-a", "SIGNED"),
            _v(1, "lead-b", "SIGNED"),
            _v(2, "qc-inspector", "SIGNED"),
        ),
        seats=SEATS,
    )
    assert verdict.complete is False
    assert any("席位 1" in p and "超过一个" in p for p in verdict.problems)


def test_consumed_group_cannot_be_reused() -> None:
    """消费后整组一次性失效，不可重放。"""
    verdict = evaluate_countersignature(
        action_key=ACTION,
        events=(
            _v(1, "delivery-lead", "SIGNED"),
            _v(2, "qc-inspector", "SIGNED"),
            _v(1, "delivery-lead", "CONSUMED"),
        ),
        seats=SEATS,
    )
    assert verdict.complete is False
    assert any("CONSUMED" in p and "不可重放" in p for p in verdict.problems)


def test_out_of_range_seat_is_rejected() -> None:
    """席位变更后残留的旧签署（越界席位）→ 报红。"""
    verdict = evaluate_countersignature(
        action_key=ACTION,
        events=(_v(1, "delivery-lead"), _v(2, "qc"), _v(9, "ghost")),
        seats=SEATS,
    )
    assert verdict.complete is False
    assert any("越界席位 9" in p for p in verdict.problems)


def test_empty_seats_fail_closed() -> None:
    """席位集合为空（契约错误）→ 不 complete，不是 vacuous truth。"""
    verdict = evaluate_countersignature(action_key="ANY", events=(), seats=())
    assert verdict.complete is False
