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

# 必须仍然是 BLOCKED 的条目：把状态改成 LIFTED 同样要显式改这里。
# 只列 S1 这一条——TC-1~TC-5 的裁决 2026-11-23 已签（LIFTED + DECISION）；
# 原始冻结决议于 2026-11-25 入仓并登记（LIFTED + ALL，台账
# V205-P0-FREEZE-RESOLUTION-REGISTER）。两次解除都是从这份白名单里**显式**删掉的，
# 不是靠改断言口径绕过的。
# 「存在性」与「仍是阻塞」分开断言，是因为 2026-11-25 之前这两件事被混在一条
# 断言里：一旦某条合法解除，就只能整条从白名单删掉，随后「它还在清单里」这件事
# 就再也没人检查了。
_MUST_STAY_BLOCKED = ("V-BLOCK-R42-IMPLEMENTATION-AUTH",)


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


def test_must_stay_blocked_blocks_are_not_silently_lifted() -> None:
    """这两条本轮仍必须是 BLOCKED；改成 LIFTED 要显式改白名单，不能顺手带过。

    与上一条分开放，是因为「存在」和「仍阻塞」是两个不同的承诺。上一轮把它们
    压在一条断言里，TC-1~TC-5 合法解除时唯一的选择就是整条移出白名单——于是
    「它们还在清单里」这件事也随之失去检查。解除本身也不是免费的：清单的判据
    会要求 lifted_by / lift_evidence 互为凭据，这条断言只防「连凭据都没有就改状态」。
    """
    still_blocked = set(open_block_ids(load_registry()))
    lifted_quietly = set(_MUST_STAY_BLOCKED) - still_blocked
    assert not lifted_quietly, f"疑似被静默解除: {sorted(lifted_quietly)}"


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
