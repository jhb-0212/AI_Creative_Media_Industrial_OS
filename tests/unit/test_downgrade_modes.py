"""ADR-R46 §8 裁决二：降级模式声明与**仓库内**证据的一致性断言。

口径：
1. 未在 ``downgrade_modes.json`` 登记的 revision 默认为 ``guarded``——必须由 CI 往返矩阵证明；
2. ``manual``（需人工确认）与 ``unsupported``（禁止自动降级）必须显式登记，且两集合不相交；
3. 登记为 manual/unsupported 的 revision 不得同时出现在「P→U→D 已证明」集合里——
   声明与证据冲突时以证据为准并降级声明。

证据必须来自**仓库内**文件（``docs/audits/…bidirectional-snapshot.json``）。
此前这里读的是仓库外 ``../runs/…/strip-sweep.results.json``，文件不存在时静默返回
空集，全部断言对空集成立——CI 里这条测试**永远不可能失败**，等于没有。
"""

from __future__ import annotations

import json
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_MODES = _ROOT / "data" / "migrations" / "downgrade_modes.json"
_MATRIX = _ROOT / "docs" / "audits" / "2026-11-28-wp04-reversibility-matrix.json"
_SNAPSHOT = _ROOT / "docs" / "audits" / "2026-11-28-wp04-bidirectional-snapshot.json"

_ALLOWED_MODES = ("manual", "unsupported")
_PROVEN_VERDICT = "no_restore_proven"


def _modes() -> dict[str, list[str]]:
    payload = json.loads(_MODES.read_text(encoding="utf-8"))
    return {key: list(payload.get(key, [])) for key in _ALLOWED_MODES}


def _chain() -> set[str]:
    payload = json.loads(_MATRIX.read_text(encoding="utf-8"))
    return set(payload["chain_coverage"])


def _proven_revisions() -> set[str]:
    """仓库内快照里 verdict 为 no_restore_proven 的 revision（实测已证明往返）。"""
    payload = json.loads(_SNAPSHOT.read_text(encoding="utf-8"))
    return {
        str(row["revision"]) for row in payload["results"] if row.get("verdict") == _PROVEN_VERDICT
    }


def test_evidence_files_exist_in_repo() -> None:
    """证据必须随仓库走：文件缺失即失败，不允许「缺了就当成空集通过」。"""
    assert _MODES.exists()
    assert _MATRIX.exists()
    assert _SNAPSHOT.exists()


def test_proven_set_is_not_empty() -> None:
    """防止断言对空集空转：快照必须有内容，且至少一批被证明。"""
    proven = _proven_revisions()
    assert len(proven) >= 10, f"快照里的已证明 revision 过少：{sorted(proven)}"


def test_proven_revisions_are_on_the_chain() -> None:
    chain = _chain()
    unknown = sorted(_proven_revisions() - chain)
    assert not unknown, f"快照里的 revision 不在迁移链上（证据漂移）：{unknown}"


def test_declared_modes_are_disjoint() -> None:
    modes = _modes()
    assert set(modes["manual"]) & set(modes["unsupported"]) == set()


def test_declared_revisions_exist_in_chain() -> None:
    chain = _chain()
    for mode, revisions in _modes().items():
        for revision in revisions:
            assert revision in chain, f"{mode} 登记的 {revision} 不在迁移链上"


def test_declared_revisions_are_not_silently_proven_reversible() -> None:
    proven = _proven_revisions()
    for mode, revisions in _modes().items():
        overlap = set(revisions) & proven
        assert not overlap, f"{mode} 与实测已证明往返冲突：{sorted(overlap)}"


def test_modes_file_covers_only_known_keys() -> None:
    payload = json.loads(_MODES.read_text(encoding="utf-8"))
    payload.pop("_doc", None)
    assert set(payload) == set(_ALLOWED_MODES)
