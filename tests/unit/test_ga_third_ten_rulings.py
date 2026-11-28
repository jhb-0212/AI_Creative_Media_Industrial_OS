"""#35 G-A 第三轮十项裁定的回归测试：正向锁定 + 负向证伪。

十项裁定（所有者 2026-11-24，基线取证见 contracts/enums/status_field_taxonomy.yaml）：
- CreativeProposal → lifecycle：基线 §2.4 原文给出六态字面值，登记权威状态机；
- 其余九项 → envelope_only，basis 分别为 immutable_record（8）与 derived_projection（1）。

台账 §3.2/§3.3 的建议在本轮九项上均是错向导：权利事实、审批事件、交付链接有效期、
ModerationCase 处置、TimelineDraft 权威状态都被它误读成对象生命周期。测试把这些
「不得回落」的结论逐项锁死，并对注入的坏状态精确断言问题文本（ERROR≠FAILED）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import contract_check as cc  # noqa: E402
import gen_status_contracts as gs  # noqa: E402

SCHEMA_PATH = ROOT / "contracts" / "schemas" / "object_schemas.schema.json"
SM_PATH = ROOT / "contracts" / "state_machines" / "state_machines.yaml"
TAXONOMY_PATH = ROOT / "contracts" / "enums" / "status_field_taxonomy.yaml"
LAYERS_PATH = ROOT / "contracts" / "enums" / "status_layers.yaml"

PROPOSAL_STATES = {"DRAFT", "PRECHECKED", "CANDIDATE", "SELECTED", "REJECTED", "ARCHIVED"}

# 本轮九项 envelope_only 裁定及其依据类别，逐项锁定，防止「同构复用」建议偷渡回来。
PROJECTION_RULINGS = {
    "CharacterVoiceProfile": "immutable_record",
    "DeliverySpec": "immutable_record",
    "EscapeReport": "immutable_record",
    "IntentContract": "immutable_record",
    "ProjectProductionProfile": "immutable_record",
    "Report": "immutable_record",
    "ShotProductionProfile": "immutable_record",
    "TimelineManifest": "derived_projection",
    "ToolExchangePackage": "immutable_record",
}
THIRD_ROUND = {*PROJECTION_RULINGS, "CreativeProposal"}


def _definitions() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))["definitions"]


def _sm_objects() -> list[dict[str, Any]]:
    return yaml.safe_load(SM_PATH.read_text(encoding="utf-8"))["objects"]


def _taxonomy() -> dict[str, Any]:
    return yaml.safe_load(TAXONOMY_PATH.read_text(encoding="utf-8"))


def _classes() -> dict[str, str]:
    return {str(e["object"]): str(e["class"]) for e in _taxonomy()["objects"]}


def _sm(object_name: str) -> dict[str, Any]:
    for entry in _sm_objects():
        if entry["object"] == object_name:
            return entry
    raise AssertionError(f"{object_name} 未在状态机登记")


def _edges(object_name: str) -> set[tuple[str, str]]:
    return {(str(e["from"]), str(e["to"])) for e in _sm(object_name)["transitions"]}


def _sentinel() -> str:
    return cc.envelope_only_sentinel(yaml.safe_load(LAYERS_PATH.read_text(encoding="utf-8")))


# --------------------------------------------------------------------------- #
# 正向：裁定已反映到状态机 / 分类台账 / 派生 Schema，且缺口清零
# --------------------------------------------------------------------------- #
def test_current_contract_is_consistent() -> None:
    assert cc.check_status_enum_consistency() is True
    assert gs.drift_problems() == []


def test_no_lifecycle_gap_remains() -> None:
    assert {e["object"] for e in _taxonomy()["objects"] if e["class"] == "lifecycle_gap"} == set()
    problems = cc.taxonomy_problems(
        _taxonomy()["objects"],
        cc.state_machine_states(_sm_objects()),
        _definitions(),
        basis_kinds=set(_taxonomy()["basis_kinds"]),
    )
    assert problems == []


@pytest.mark.parametrize(("name", "basis"), sorted(PROJECTION_RULINGS.items()))
def test_projection_rulings_are_locked(name: str, basis: str) -> None:
    entry = next(e for e in _taxonomy()["objects"] if e["object"] == name)
    assert entry["class"] == "envelope_only"
    assert entry["basis"] == basis
    assert entry["reason"].strip() and entry["unblocks_when"].strip()
    assert name not in {o["object"] for o in _sm_objects()}
    assert _definitions()[name]["properties"]["status"]["enum"] == [_sentinel()]


def test_proposal_uses_baseline_six_literals() -> None:
    """§2.4 的六值一个不能少，台账的「待评审→采纳/否决」不得混入。"""
    sm = _sm("CreativeProposal")
    assert set(sm["allowed_states"]) == PROPOSAL_STATES
    assert not {"PENDING", "IN_REVIEW"} & set(sm["allowed_states"])
    assert sm["initial_state"] == "DRAFT"
    assert _classes()["CreativeProposal"] == "lifecycle"
    assert set(_definitions()["CreativeProposal"]["properties"]["status"]["enum"]) == (
        PROPOSAL_STATES
    )


def test_proposal_edges_are_fail_closed() -> None:
    edges = _edges("CreativeProposal")
    assert {
        ("DRAFT", "PRECHECKED"),
        ("PRECHECKED", "CANDIDATE"),
        ("CANDIDATE", "SELECTED"),
        ("CANDIDATE", "REJECTED"),
        ("SELECTED", "ARCHIVED"),
        ("REJECTED", "ARCHIVED"),
    } == edges
    # SELECTED 还能派生正式版本，因此不是冻结态；归档与否决不再长出子对象。
    assert set(_sm("CreativeProposal")["frozen_states"]) == {"ARCHIVED", "REJECTED"}
    assert all(src != "ARCHIVED" for src, _ in edges)


# --------------------------------------------------------------------------- #
# 负向：闸门对注入的坏状态必须精确报 FAILED（不是夹具 ERROR）
# --------------------------------------------------------------------------- #
def test_reverting_projection_to_debt_is_reported() -> None:
    objects = [
        {k: v for k, v in e.items() if k != "basis"} | {"class": "lifecycle_gap"}
        if e["object"] == "ToolExchangePackage"
        else e
        for e in _taxonomy()["objects"]
    ]
    problems = cc.taxonomy_problems(objects, cc.state_machine_states(_sm_objects()), _definitions())
    assert any("ToolExchangePackage" in p and "缺债务字段" in p for p in problems), problems


def test_projection_with_state_machine_is_reported() -> None:
    sm_states = cc.state_machine_states(_sm_objects())
    sm_states["Report"] = {"PENDING", "RESOLVED"}
    problems = cc.taxonomy_problems(_taxonomy()["objects"], sm_states, _definitions())
    assert any("Report" in p and "两套并存" in p for p in problems), problems


def test_projection_with_invented_lifecycle_enum_is_reported() -> None:
    definition = json.loads(json.dumps(_definitions()["ToolExchangePackage"]))
    definition["properties"]["status"]["enum"] = ["VALID", "EXPIRED", "REVOKED"]
    problems = cc.schema_enum_problems(
        {"ToolExchangePackage": definition},
        cc.state_machine_states(_sm_objects()),
        _classes(),
        yaml.safe_load(LAYERS_PATH.read_text(encoding="utf-8")),
    )
    assert any("哨兵" in p and "VALID" in p for p in problems), problems


def test_wrong_basis_is_reported() -> None:
    objects = [
        {**e, "basis": "derived_projecton"} if e["object"] == "TimelineManifest" else e
        for e in _taxonomy()["objects"]
    ]
    problems = cc.taxonomy_problems(
        objects,
        cc.state_machine_states(_sm_objects()),
        _definitions(),
        basis_kinds=set(_taxonomy()["basis_kinds"]),
    )
    assert any("TimelineManifest" in p and "basis_kinds" in p for p in problems), problems


def test_missing_taxonomy_entry_is_reported() -> None:
    objects = [e for e in _taxonomy()["objects"] if e["object"] not in THIRD_ROUND]
    problems = cc.taxonomy_problems(objects, cc.state_machine_states(_sm_objects()), _definitions())
    for name in THIRD_ROUND:
        assert any(name in p and "没在 status_field_taxonomy 登记分类" in p for p in problems)


def test_proposal_without_state_machine_is_reported() -> None:
    objects = [
        {**e, "class": "lifecycle"} if e["object"] == "CreativeProposal" else e
        for e in _taxonomy()["objects"]
    ]
    sm_states = cc.state_machine_states(
        [o for o in _sm_objects() if o["object"] != "CreativeProposal"]
    )
    problems = cc.taxonomy_problems(objects, sm_states, _definitions())
    assert any("CreativeProposal" in p and "没有它的状态机" in p for p in problems), problems


def test_unregistered_proposal_transition_is_reported() -> None:
    """台账曾建议的 LOCK_PENDING 若偷渡成提案状态，转移闸门必须点名报红。"""
    tampered = [o.copy() for o in _sm_objects()]
    for entry in tampered:
        if entry["object"] == "CreativeProposal":
            entry["transitions"] = [{"from": "DRAFT", "to": "LOCK_PENDING"}]
    problems = cc.transition_target_problems(tampered)
    assert any("LOCK_PENDING" in p and "未登记状态" in p for p in problems), problems


def test_simplifying_proposal_states_breaks_derived_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """把 §2.4 六值改成台账建议的三态，派生器必须报红。"""
    data = yaml.safe_load(SM_PATH.read_text(encoding="utf-8"))
    for entry in data["objects"]:
        if entry["object"] == "CreativeProposal":
            entry["allowed_states"] = ["DRAFT", "PENDING", "APPROVED", "REJECTED"]
    tampered = tmp_path / "state_machines.yaml"
    tampered.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    monkeypatch.setattr(gs, "STATE_MACHINES", tampered)
    problems = gs.drift_problems()
    assert problems, "把正文六值简化为三态后派生文件仍被判一致，闸门失明"
    assert any("CreativeProposal" in p for p in problems), problems
