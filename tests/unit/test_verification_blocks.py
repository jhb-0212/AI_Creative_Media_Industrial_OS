"""``[V-BLOCKED]`` 阻塞清单判据的负向用例（证明它会报红）。

为什么一个「清单校验」也要有负向用例
------------------------------------
这类判据最容易变成装饰：写得像检查，实际任何输入都返回空问题列表。红不了一次的
闸门，等于没有。所以每条判据都用一份**故意写坏**的清单喂进去，断言它确实报红、
且报错指向真正的毛病（不是靠「字段缺失」把别的问题也顺带盖住）。

最后一组用例防守另一种失真：**真清单被悄悄删条目**。删掉一条阻塞不会让任何
判据变红（counts 跟着改，字段仍齐全），但会让「还有哪些阻塞」这个答案永久变少。
代价是删条目必须同时改一份白名单，也就是必须是一次明确的 diff。
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

_REQUIRED_KINDS = (
    "V-BLOCK-ORIGINAL-FREEZE-RESOLUTION",
    "V-BLOCK-R42-IMPLEMENTATION-AUTH",
    "V-BLOCK-TC-1",
    "V-BLOCK-TC-2",
    "V-BLOCK-TC-3",
    "V-BLOCK-TC-4",
    "V-BLOCK-TC-5",
)


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
    lifted = _block(
        status=LIFTED,
        lifted_at="2026-11-25",
        lifted_by="V205-TEST-LIFT",
        lift_evidence="doc.md",
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
    lifted = _block(
        status=LIFTED,
        lifted_at="2026-11-25",
        lifted_by="V205-NOT-THERE",
        lift_evidence="doc.md",
    )
    problems = _check(_registry(lifted), workspace)
    assert any("台账修订不存在" in p for p in problems)


def test_lift_evidence_must_mention_the_amendment(workspace: pathlib.Path) -> None:
    """裁决 id 与证据文档必须互为凭据：只写一头就会出现「有记录无裁决」。"""
    (workspace / "doc.md").write_text("本文没提到那条裁决", encoding="utf-8")
    lifted = _block(
        status=LIFTED,
        lifted_at="2026-11-25",
        lifted_by="V205-TEST-LIFT",
        lift_evidence="doc.md",
    )
    problems = _check(_registry(lifted), workspace)
    assert any("找不到裁决 id" in p for p in problems)


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


def test_critical_blocks_are_not_silently_removed() -> None:
    """这七条仍在阻塞；删任何一条都必须同时改这份白名单（即一次明确的 diff）。

    为什么不用 counts 判据代替：counts 可以跟着一起改。删条目本身不违反任何
    字段规则，闸门因此会保持绿色——这条断言是唯一能拦住「悄悄少一条」的地方。
    """
    present = set(open_block_ids(load_registry()))
    assert set(_REQUIRED_KINDS) <= present, f"疑似被静默删除: {set(_REQUIRED_KINDS) - present}"


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
