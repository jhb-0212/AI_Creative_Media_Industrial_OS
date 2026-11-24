"""#35 G-A 第二轮四项裁定的回归测试：正向锁定 + 负向证伪。

四项裁定（所有者 2026-11-24，见 docs/audits/2026-11-24-status-governance-ruling-packet.md）：
- CameraRig → envelope_only + child_structure（机位属于 PrevizVersion）；
- CharacterReferenceSet → envelope_only + immutable_record（集合不可变版本）；
- ShotResourceAllocation → envelope_only + derived_projection（金额由预算账本汇总）；
- ShotTierChangeRequest → lifecycle（基线 17.7 的十个字面值 + 授权转移边）。

负向测试用**函数级注入 + 断言 problems**，不靠「跑脚本退出码非 0」——后者会把
夹具 ERROR 与断言 FAILED 混为一谈（ERROR≠FAILED 判据）。
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

TIER_STATES = {
    "DRAFT",
    "SUBMITTED",
    "EVALUATING",
    "APPROVED",
    "REJECTED",
    "EXECUTING",
    "COMPLETED",
    "CANCELLED",
    "FAILED",
    "ROLLED_BACK",
}
TIER_TERMINALS = {"COMPLETED", "REJECTED", "CANCELLED", "FAILED", "ROLLED_BACK"}


def _definitions() -> dict[str, Any]:
    data = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    return data["definitions"]


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


# --------------------------------------------------------------------------- #
# 正向：裁定已反映到三份事实源（权威状态机 / 分类台账 / 派生 Schema）
# --------------------------------------------------------------------------- #
def test_current_contract_is_consistent() -> None:
    assert cc.check_status_enum_consistency() is True
    assert gs.drift_problems() == []


@pytest.mark.parametrize(
    ("name", "basis"),
    [
        ("CameraRig", "child_structure"),
        ("CharacterReferenceSet", "immutable_record"),
        ("ShotResourceAllocation", "derived_projection"),
    ],
)
def test_projection_objects_use_sentinel(name: str, basis: str) -> None:
    entry = next(e for e in _taxonomy()["objects"] if e["object"] == name)
    assert entry["class"] == "envelope_only"
    assert entry["basis"] == basis
    assert entry["reason"].strip() and entry["unblocks_when"].strip()
    assert name not in {o["object"] for o in _sm_objects()}
    sentinel = cc.envelope_only_sentinel(
        yaml.safe_load(LAYERS_PATH.read_text(encoding="utf-8"))
    )
    assert _definitions()[name]["properties"]["status"]["enum"] == [sentinel]


def test_tier_change_request_has_full_baseline_value_set() -> None:
    """17.7 的十个字面值一个都不能少；台账的三态简化不得混入。"""
    sm = _sm("ShotTierChangeRequest")
    assert set(sm["allowed_states"]) == TIER_STATES
    assert "PENDING" not in sm["allowed_states"]
    assert sm["initial_state"] == "DRAFT"
    assert set(_definitions()["ShotTierChangeRequest"]["properties"]["status"]["enum"]) == (
        TIER_STATES
    )


def test_tier_change_transition_edges_follow_baseline_and_are_fail_closed() -> None:
    edges = _edges("ShotTierChangeRequest")
    assert {
        ("DRAFT", "SUBMITTED"),
        ("SUBMITTED", "EVALUATING"),
        ("EVALUATING", "APPROVED"),
        ("EVALUATING", "REJECTED"),
        ("APPROVED", "EXECUTING"),
        ("EXECUTING", "COMPLETED"),
    } <= edges
    # 三个旁路状态只从语义上说得通的位置进入；正文只写「另支持」未给边，
    # 因此这些边在 note 里标为所有者推导，registration_status 仍待复核。
    assert {("EXECUTING", s) for s in ("FAILED", "ROLLED_BACK")} <= edges
    assert {("DRAFT", "CANCELLED"), ("APPROVED", "CANCELLED")} <= edges
    # 终态不可原地复活（31.5 红线），note 与转移图必须同口径。
    assert not any(src in TIER_TERMINALS for src, _ in edges)
    assert set(_sm("ShotTierChangeRequest")["frozen_states"]) == TIER_TERMINALS


# --------------------------------------------------------------------------- #
# 负向：闸门对注入的坏状态必须精确报 FAILED（不是夹具 ERROR）
# --------------------------------------------------------------------------- #
def test_lifecycle_object_without_state_machine_is_reported() -> None:
    objects = [
        {**e, "class": "lifecycle"} if e["object"] == "ShotTierChangeRequest" else e
        for e in _taxonomy()["objects"]
    ]
    sm_states = cc.state_machine_states(
        [o for o in _sm_objects() if o["object"] != "ShotTierChangeRequest"]
    )
    problems = cc.taxonomy_problems(objects, sm_states, _definitions())
    assert any("ShotTierChangeRequest" in p and "没有它的状态机" in p for p in problems), problems


@pytest.mark.parametrize("name", ["CameraRig", "CharacterReferenceSet", "ShotResourceAllocation"])
def test_projection_object_with_state_machine_is_reported(name: str) -> None:
    sm_states = cc.state_machine_states(_sm_objects())
    sm_states[name] = {"NO_LIFECYCLE"}
    problems = cc.taxonomy_problems(_taxonomy()["objects"], sm_states, _definitions())
    assert any(name in p and "两套并存" in p for p in problems), problems


def test_projection_object_with_wrong_basis_is_reported() -> None:
    objects = [
        {**e, "basis": "immutabel_record"} if e["object"] == "CharacterReferenceSet" else e
        for e in _taxonomy()["objects"]
    ]
    problems = cc.taxonomy_problems(
        objects,
        cc.state_machine_states(_sm_objects()),
        _definitions(),
        basis_kinds=set(_taxonomy()["basis_kinds"]),
    )
    assert any("CharacterReferenceSet" in p and "basis_kinds" in p for p in problems), problems


def test_projection_object_with_lifecycle_enum_is_reported() -> None:
    definition = json.loads(json.dumps(_definitions()["ShotResourceAllocation"]))
    definition["properties"]["status"]["enum"] = ["REQUESTED", "RESERVED"]
    classes = {**_classes(), "ShotResourceAllocation": cc.CLASS_ENVELOPE_ONLY}
    problems = cc.schema_enum_problems(
        {"ShotResourceAllocation": definition},
        cc.state_machine_states(_sm_objects()),
        classes,
        yaml.safe_load(LAYERS_PATH.read_text(encoding="utf-8")),
    )
    assert any("哨兵" in p and "REQUESTED" in p for p in problems), problems


def test_unregistered_transition_endpoint_is_reported() -> None:
    tampered = [o.copy() for o in _sm_objects()]
    for entry in tampered:
        if entry["object"] == "ShotTierChangeRequest":
            entry["transitions"] = [{"from": "DRAFT", "to": "GOLD"}]
    problems = cc.transition_target_problems(tampered)
    assert any("GOLD" in p and "未登记状态" in p for p in problems), problems


def test_simplifying_baseline_value_set_breaks_derived_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """把正文十值改成台账建议的三态，派生器必须报红——防止「简化」偷渡。"""
    data = yaml.safe_load(SM_PATH.read_text(encoding="utf-8"))
    for entry in data["objects"]:
        if entry["object"] == "ShotTierChangeRequest":
            entry["allowed_states"] = ["DRAFT", "PENDING", "APPROVED", "REJECTED"]
    tampered = tmp_path / "state_machines.yaml"
    tampered.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    monkeypatch.setattr(gs, "STATE_MACHINES", tampered)

    problems = gs.drift_problems()
    assert problems, "把基线十值简化为三态后派生文件仍被判一致，闸门失明"
    assert any("ShotTierChangeRequest" in p for p in problems), problems
