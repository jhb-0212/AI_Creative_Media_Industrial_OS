"""``[V-BLOCKED]`` 阻塞清单判据的负向用例（证明它会报红）。

为什么一个「清单校验」也要有负向用例
------------------------------------
这类判据最容易变成装饰：写得像检查，实际任何输入都返回空问题列表。红不了一次的
闸门，等于没有。所以每条判据都用一份**故意写坏**的清单喂进去，断言它确实报红、
且报错指向真正的毛病（不是靠「字段缺失」把别的问题也顺带盖住）。

最后一组用例防守另一种失真：**真清单被悄悄删条目或悄悄改状态**。删掉一条阻塞不会
让任何判据变红（counts 跟着改，字段仍齐全），但会让「还有哪些阻塞」这个答案永久变少；
把 BLOCKED 改成 LIFTED 同样不会红——只要补上解除字段就行。代价是这两类改动必须
同时改两份白名单（存在性 + 必须仍阻塞），也就是必须是一次明确的 diff。
"""

from __future__ import annotations

import pathlib
from typing import Any

import pytest

from scripts.verification_blocks import (
    BLOCKED,
    LIFTED,
    check_registry,
    load_registry,
    open_block_ids,
)

# 清单里必须一直存在的条目：不论状态，删掉任何一条都要显式改这里。
_REQUIRED_IDS = (
    "V-BLOCK-ORIGINAL-FREEZE-RESOLUTION",
    "V-BLOCK-R42-IMPLEMENTATION-AUTH",
    "V-BLOCK-TC-1",
    "V-BLOCK-TC-2",
    "V-BLOCK-TC-3",
    "V-BLOCK-TC-4",
    "V-BLOCK-TC-5",
    "V-BLOCK-P2-MEDIA-TYPE-ABANDONED",
)

# 此刻应当仍处于 BLOCKED 的条目全集。**空元组是有内容的断言，不是放行**。
#
# 三次解除都是从这份白名单里**显式**删掉的，不是靠改口径绕过的：
#   TC-1~TC-5（2026-11-23 裁决，LIFTED + DECISION）
#   → 原始冻结决议（2026-11-25 入仓，LIFTED + ALL，V205-P0-FREEZE-RESOLUTION-REGISTER）
#   → S1（2026-11-25 Owner 明确认可，LIFTED + DECISION，V205-R42-S1-AUTHORIZATION-ACK）。
#
# 断言用**相等**而不是「子集」：只断言子集的话，空元组会变成恒真式——之后任何人新增
# 一条阻塞、或把某条改回 BLOCKED，都无人察觉。相等把「当下为 0」变成一条需要签字的状态。
# 「存在性」与「仍是阻塞」仍是两条独立断言：一旦某条合法解除就只能移出后者，而前者
# （_REQUIRED_IDS）继续保证它还在清单里——2026-11-25 之前这两件事压在一条断言里，
# 解除时只能整条删掉，此后「它还在清单里」就再没人检查了。
_MUST_STAY_BLOCKED: tuple[str, ...] = ()


def _block(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "V-BLOCK-TEST",
        "title": "测试条目",
        "status": BLOCKED,
        "raised_by": "doc.md",
        "raised_at": "2026-11-25",
        "evidence": ["doc.md"],
        "gap": "还缺一个决定",
        "unblocks_when": "Owner 说一句",
        "decision_owner": "Owner",
    }
    base.update(overrides)
    return base


def _lifted(**overrides: Any) -> dict[str, Any]:
    """一份合法的最小 LIFTED 条目（scope=ALL，故不需要 remaining_gate）。"""
    base: dict[str, Any] = {
        "status": LIFTED,
        "lifted_at": "2026-11-25",
        "lifted_by": "V205-TEST-LIFT",
        "lift_evidence": "doc.md",
        "lifted_scope": "ALL",
    }
    base.update(overrides)
    return _block(**base)


def _registry(*blocks: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": 1,
        "counts": {
            "total": len(blocks),
            "blocked": sum(1 for b in blocks if b["status"] == BLOCKED),
            "lifted": sum(1 for b in blocks if b["status"] == LIFTED),
        },
        "blocks": list(blocks),
    }


@pytest.fixture()
def workspace(tmp_path: pathlib.Path) -> pathlib.Path:
    """一套最小可用的仓库：一份文档 + 一份台账。"""
    (tmp_path / "doc.md").write_text("依据见 V205-TEST-LIFT", encoding="utf-8")
    (tmp_path / "amendments.yaml").write_text(
        "amendments:\n  - id: 'V205-TEST-LIFT'\n  - id: 'V205-OTHER'\n",
        encoding="utf-8",
    )
    return tmp_path


def _check(registry: dict[str, Any], workspace: pathlib.Path) -> list[str]:
    return check_registry(
        registry,
        root=workspace,
        amendments_path=workspace / "amendments.yaml",
    )


def test_valid_blocked_entry_passes(workspace: pathlib.Path) -> None:
    assert _check(_registry(_block()), workspace) == []


def test_valid_lifted_entry_passes(workspace: pathlib.Path) -> None:
    assert _check(_registry(_lifted()), workspace) == []


def test_decided_lift_with_gate_passes(workspace: pathlib.Path) -> None:
    """决策层解除 + remaining_gate：这是 TC-1~TC-5 的合法形态。"""
    lifted = _lifted(
        lifted_scope="DECISION",
        remaining_gate={"summary": "PoC 未启动，切换须另走 ADR", "source": "doc.md"},
    )
    assert _check(_registry(lifted), workspace) == []


def test_empty_registry_is_red(workspace: pathlib.Path) -> None:
    problems = _check({"counts": {"total": 0}, "blocks": []}, workspace)
    assert any("blocks 为空" in p for p in problems)


def test_duplicate_id_is_red(workspace: pathlib.Path) -> None:
    problems = _check(_registry(_block(), _block()), workspace)
    assert any("id 重复" in p for p in problems)


def test_illegal_status_is_red(workspace: pathlib.Path) -> None:
    problems = _check(_registry(_block(status="OPEN")), workspace)
    assert any("status 非法" in p for p in problems)


def test_missing_field_is_red(workspace: pathlib.Path) -> None:
    broken = _block()
    broken.pop("unblocks_when")
    problems = _check(_registry(broken), workspace)
    assert any("unblocks_when" in p for p in problems)


def test_nonexistent_raised_by_is_red(workspace: pathlib.Path) -> None:
    problems = _check(_registry(_block(raised_by="missing.md")), workspace)
    assert any("raised_by 指向的文件不存在" in p for p in problems)


def test_nonexistent_evidence_is_red(workspace: pathlib.Path) -> None:
    problems = _check(_registry(_block(evidence=["doc.md", "gone.md"])), workspace)
    assert any("gone.md" in p for p in problems)


def test_bad_date_is_red(workspace: pathlib.Path) -> None:
    problems = _check(_registry(_block(raised_at="2026/11/25")), workspace)
    assert any("ISO 日期" in p for p in problems)


def test_blocked_with_lift_fields_is_red(workspace: pathlib.Path) -> None:
    """状态与字段矛盾必须报红：否则两种读法都能自圆其说。"""
    problems = _check(_registry(_block(lifted_at="2026-11-25")), workspace)
    assert any("BLOCKED 却带着解除字段" in p for p in problems)


def test_lifted_without_lift_fields_is_red(workspace: pathlib.Path) -> None:
    problems = _check(_registry(_block(status=LIFTED)), workspace)
    assert any("LIFTED 条目缺少字段" in p for p in problems)


def test_lifted_by_unknown_amendment_is_red(workspace: pathlib.Path) -> None:
    problems = _check(_registry(_lifted(lifted_by="V205-NOT-THERE")), workspace)
    assert any("台账修订不存在" in p for p in problems)


def test_lift_evidence_must_mention_the_amendment(workspace: pathlib.Path) -> None:
    """裁决 id 与证据文档必须互为凭据：只写一头就会出现「有记录无裁决」。"""
    (workspace / "doc.md").write_text("本文没提到那条裁决", encoding="utf-8")
    problems = _check(_registry(_lifted()), workspace)
    assert any("找不到裁决 id" in p for p in problems)


# --------------------------------------------------------------------------- #
# 解除到哪一层：DECISION 与 ALL 的字段搭配互为反证
# --------------------------------------------------------------------------- #
def test_lifted_without_scope_is_red(workspace: pathlib.Path) -> None:
    """不写 scope 就等于没说清解除了什么；读者只能自己猜。"""
    entry = _lifted()
    entry.pop("lifted_scope")
    problems = _check(_registry(entry), workspace)
    assert any("lifted_scope" in p for p in problems)


def test_decision_scope_without_gate_is_red(workspace: pathlib.Path) -> None:
    """最危险的漏写：决策解除了、门槛还在，却写成无门槛 → 被读成已放行。"""
    problems = _check(_registry(_lifted(lifted_scope="DECISION")), workspace)
    assert any("未写 remaining_gate" in p for p in problems)


def test_all_scope_with_gate_is_red(workspace: pathlib.Path) -> None:
    """反向矛盾：号称全部解除却还留着门槛，状态在自相矛盾。"""
    entry = _lifted(
        lifted_scope="ALL",
        remaining_gate={"summary": "还有门槛", "source": "doc.md"},
    )
    problems = _check(_registry(entry), workspace)
    assert any("状态自相矛盾" in p for p in problems)


def test_gate_source_must_exist(workspace: pathlib.Path) -> None:
    entry = _lifted(
        lifted_scope="DECISION",
        remaining_gate={"summary": "门槛", "source": "gone.md"},
    )
    problems = _check(_registry(entry), workspace)
    assert any("gone.md" in p for p in problems)


def test_gate_summary_is_required(workspace: pathlib.Path) -> None:
    entry = _lifted(
        lifted_scope="DECISION",
        remaining_gate={"source": "doc.md"},
    )
    problems = _check(_registry(entry), workspace)
    assert any("remaining_gate 缺少字段" in p for p in problems)


def test_blocked_with_gate_is_red(workspace: pathlib.Path) -> None:
    """BLOCKED 条目写「还没解除什么」没有意义，且会让读者以为已部分解除。"""
    entry = _block(remaining_gate={"summary": "x", "source": "doc.md"})
    problems = _check(_registry(entry), workspace)
    assert any("BLOCKED 却带着解除字段" in p for p in problems)


# --------------------------------------------------------------------------- #
# 引用计数：必须锚定到提交，不能对工作区裸 grep
# --------------------------------------------------------------------------- #
def test_reference_baseline_passes_when_anchored(workspace: pathlib.Path) -> None:
    entry = _block(
        reference_baseline={
            "as_of_commit": "57bcc46",
            "command": 'git grep -c "AFS-P0-FREEZE-2026-11-13-01" 57bcc46',
            "matched_lines": 20,
            "files": 7,
        }
    )
    assert _check(_registry(entry), workspace) == []


def test_reference_baseline_command_must_pin_commit(workspace: pathlib.Path) -> None:
    """对工作区直接 grep 会数进未跟踪副本与测试产物——17/20/23/25 就是这么来的。"""
    entry = _block(
        reference_baseline={
            "as_of_commit": "57bcc46",
            "command": 'grep -rn "AFS-P0-FREEZE-2026-11-13-01" .',
            "matched_lines": 20,
            "files": 7,
        }
    )
    problems = _check(_registry(entry), workspace)
    assert any("没有锚定到 as_of_commit" in p for p in problems)


def test_reference_baseline_files_cannot_exceed_lines(workspace: pathlib.Path) -> None:
    entry = _block(
        reference_baseline={
            "as_of_commit": "57bcc46",
            "command": 'git grep -c "X" 57bcc46',
            "matched_lines": 3,
            "files": 7,
        }
    )
    problems = _check(_registry(entry), workspace)
    assert any("口径不可能成立" in p for p in problems)


def test_reference_baseline_requires_commit_field(workspace: pathlib.Path) -> None:
    entry = _block(
        reference_baseline={
            "command": 'git grep -c "X" 57bcc46',
            "matched_lines": 3,
            "files": 1,
        }
    )
    problems = _check(_registry(entry), workspace)
    assert any("reference_baseline 缺少字段" in p for p in problems)


def test_counts_mismatch_is_red(workspace: pathlib.Path) -> None:
    registry = _registry(_block())
    registry["counts"]["total"] = 9
    problems = _check(registry, workspace)
    assert any("counts.total" in p for p in problems)


# --------------------------------------------------------------------------- #
# 真清单：必须通过，且关键阻塞不得被静默删除
# --------------------------------------------------------------------------- #
def test_repository_registry_passes() -> None:
    assert check_registry(load_registry()) == []


def test_required_blocks_are_not_silently_removed() -> None:
    """这八条必须一直留在清单里（不论状态）；删任何一条都要显式改这份白名单。

    为什么不用 counts 判据代替：counts 可以跟着一起改。删条目本身不违反任何
    字段规则，闸门因此会保持绿色——这条断言是唯一能拦住「悄悄少一条」的地方。
    注意它**不管状态**：解除一条阻塞不该让这条断言失败，否则合法解除只能靠
    把 id 从白名单里删掉来完成，而删掉之后「它还在清单里」就再没人检查了。
    """
    present = {str(item.get("id")) for item in load_registry()["blocks"]}
    missing = set(_REQUIRED_IDS) - present
    assert not missing, f"疑似被静默删除: {sorted(missing)}"


def test_blocked_set_matches_whitelist_exactly() -> None:
    """清单当前仍阻塞的条目必须**恰好等于**白名单；此刻两者都是空集。

    与上一条分开放，是因为「存在」和「仍阻塞」是两个不同的承诺。上一轮把它们
    压在一条断言里，TC-1~TC-5 合法解除时唯一的选择就是整条移出白名单——于是
    「它们还在清单里」这件事也随之失去检查。

    为什么判相等而不是判子集：白名单清空后，子集断言恒真，等于这条用例从此消失。
    判相等则两侧都要动——新增一条阻塞、或把已解除的改回 BLOCKED，都会在 CI 里留下
    一次必须解释的 diff。0 不是「没人管」，是一条被断言的当前状态。

    解除也不是免费的：清单判据会要求 lifted_by / lift_evidence 互为凭据，本条只防
    「白名单没跟着改」这一种情况。
    """
    still_blocked = set(open_block_ids(load_registry()))
    assert still_blocked == set(_MUST_STAY_BLOCKED), (
        f"仍阻塞集合与白名单不符：新增 {sorted(still_blocked - set(_MUST_STAY_BLOCKED))}，"
        f"已解除 {sorted(set(_MUST_STAY_BLOCKED) - still_blocked)}"
    )


def test_p2_is_lifted_in_repository_registry() -> None:
    """P2 的解除必须留在清单里（带 lifted_by），而不是删掉。

    删掉等于抹去「曾经阻塞过、后来由谁解除」，下一次同类复核会从零开始找依据。
    """
    lifted = {
        str(item.get("id")): item
        for item in load_registry()["blocks"]
        if str(item.get("status")) == LIFTED
    }
    assert "V-BLOCK-P2-MEDIA-TYPE-ABANDONED" in lifted
    assert lifted["V-BLOCK-P2-MEDIA-TYPE-ABANDONED"]["lifted_by"] == "V205-R42-P2-BLOCK-LIFT"
