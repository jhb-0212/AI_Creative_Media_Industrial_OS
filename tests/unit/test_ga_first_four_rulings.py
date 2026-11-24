"""#35 G-A 首轮四项裁定的回归测试：正向锁定 + 负向证伪。

四项裁定（所有者 2026-11-24，见 docs/audits/2026-11-24-status-governance-ruling-packet.md）：
- BlockingAsset / ControlSignalAsset → envelope_only + derived_projection（哨兵 status）；
- ContinuityConstraint → lifecycle（包级五值 + PENDING_CONFIRMATION 前置态）；
- ExternalToolArtifact → lifecycle（独立暂存状态机，PROMOTED 不等于 APPROVED/GOLD）。

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


def _definitions() -> dict[str, Any]:
    data = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    return data["definitions"]


def _sm_objects() -> list[dict[str, Any]]:
    return yaml.safe_load(SM_PATH.read_text(encoding="utf-8"))["objects"]


def _taxonomy() -> dict[str, Any]:
    return yaml.safe_load(TAXONOMY_PATH.read_text(encoding="utf-8"))


def _classes() -> dict[str, str]:
    return {str(e["object"]): str(e["class"]) for e in _taxonomy()["objects"]}


def _entry(object_name: str) -> dict[str, Any]:
    for entry in _taxonomy()["objects"]:
        if entry["object"] == object_name:
            return entry
    raise AssertionError(f"{object_name} 未在分类台账登记")


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


@pytest.mark.parametrize("name", ["BlockingAsset", "ControlSignalAsset"])
def test_projection_objects_use_sentinel(name: str) -> None:
    entry = _entry(name)
    assert entry["class"] == "envelope_only"
    assert entry["basis"] == "derived_projection"
    assert entry["reason"].strip() and entry["unblocks_when"].strip()
    assert name not in {o["object"] for o in _sm_objects()}
    sentinel = cc.envelope_only_sentinel(
        yaml.safe_load(LAYERS_PATH.read_text(encoding="utf-8"))
    )
    assert _definitions()[name]["properties"]["status"]["enum"] == [sentinel]


@pytest.mark.parametrize("name", ["ContinuityConstraint", "ExternalToolArtifact"])
def test_lifecycle_objects_have_authoritative_enum(name: str) -> None:
    assert _entry(name)["class"] == "lifecycle"
    allowed = set(_sm(name)["allowed_states"])
    schema_enum = set(_definitions()[name]["properties"]["status"]["enum"])
    assert schema_enum == allowed


def test_continuity_constraint_requires_pending_confirmation() -> None:
    """8.10：观察/推断不得直接成为硬约束，必须先落待确认队列。"""
    assert set(_sm("ContinuityConstraint")["allowed_states"]) == {
        "ACTIVE",
        "DRAFT",
        "PENDING_CONFIRMATION",
        "REVOKED",
        "STALE",
        "SUPERSEDED",
    }
    edges = _edges("ContinuityConstraint")
    assert ("DRAFT", "ACTIVE") not in edges
    assert ("DRAFT", "PENDING_CONFIRMATION") in edges
    assert ("PENDING_CONFIRMATION", "ACTIVE") in edges
    assert {("ACTIVE", s) for s in ("STALE", "SUPERSEDED", "REVOKED")} <= edges


def test_external_artifact_promotion_is_not_asset_approval() -> None:
    """暂存转正与成片级审核分层：PROMOTED 后新建 AssetVersion，不原地升级。"""
    allowed = set(_sm("ExternalToolArtifact")["allowed_states"])
    assert allowed == {"PROMOTED", "QUARANTINED", "REJECTED", "SCANNED", "STAGING", "VERIFIED"}
    assert not (allowed & {"APPROVED", "GOLD", "HUMAN_REVIEW"})
    edges = _edges("ExternalToolArtifact")
    assert ("STAGING", "SCANNED") in edges
    assert ("SCANNED", "VERIFIED") in edges
    assert ("VERIFIED", "PROMOTED") in edges
    assert ("STAGING", "QUARANTINED") in edges
    # 隔离终态不可原地复活；note 与转移图必须同口径。
    assert not any(src == "QUARANTINED" for src, _ in edges)


# --------------------------------------------------------------------------- #
# 负向：闸门对注入的坏状态必须精确报 FAILED（不是夹具 ERROR）
# --------------------------------------------------------------------------- #
def test_lifecycle_object_without_state_machine_is_reported() -> None:
    taxonomy = [
        {**e, "class": "lifecycle"} if e["object"] == "ContinuityConstraint" else e
        for e in _taxonomy()["objects"]
    ]
    sm_states = cc.state_machine_states(
        [o for o in _sm_objects() if o["object"] != "ContinuityConstraint"]
    )
    problems = cc.taxonomy_problems(taxonomy, sm_states, _definitions())
    assert any("ContinuityConstraint" in p and "没有它的状态机" in p for p in problems), problems


def test_projection_object_with_state_machine_is_reported() -> None:
    sm_states = cc.state_machine_states(_sm_objects())
    sm_states["BlockingAsset"] = {"NO_LIFECYCLE"}
    problems = cc.taxonomy_problems(_taxonomy()["objects"], sm_states, _definitions())
    assert any("BlockingAsset" in p and "两套并存" in p for p in problems), problems


def test_projection_object_with_wrong_basis_is_reported() -> None:
    taxonomy = [
        {**e, "basis": "immutabel_record"} if e["object"] == "ControlSignalAsset" else e
        for e in _taxonomy()["objects"]
    ]
    problems = cc.taxonomy_problems(
        taxonomy,
        cc.state_machine_states(_sm_objects()),
        _definitions(),
        basis_kinds=set(_taxonomy()["basis_kinds"]),
    )
    assert any("ControlSignalAsset" in p and "basis_kinds" in p for p in problems), problems


def test_projection_object_with_lifecycle_enum_is_reported() -> None:
    definition = json.loads(json.dumps(_definitions()["ControlSignalAsset"]))
    definition["properties"]["status"]["enum"] = ["DRAFT", "ACTIVE"]
    classes = {**_classes(), "ControlSignalAsset": cc.CLASS_ENVELOPE_ONLY}
    problems = cc.schema_enum_problems(
        {"ControlSignalAsset": definition},
        cc.state_machine_states(_sm_objects()),
        classes,
        yaml.safe_load(LAYERS_PATH.read_text(encoding="utf-8")),
    )
    assert any("哨兵" in p and "DRAFT" in p for p in problems), problems


def test_unregistered_transition_endpoint_is_reported() -> None:
    tampered = [o.copy() for o in _sm_objects()]
    for entry in tampered:
        if entry["object"] == "ExternalToolArtifact":
            entry["transitions"] = [{"from": "STAGING", "to": "APPROVED"}]
    problems = cc.transition_target_problems(tampered)
    assert any("APPROVED" in p and "未登记状态" in p for p in problems), problems


def test_deleting_new_state_machine_breaks_derived_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """从权威源删一条状态机，派生器必须报红——防止有人靠删条目让缺口变绿。"""
    data = yaml.safe_load(SM_PATH.read_text(encoding="utf-8"))
    data["objects"] = [o for o in data["objects"] if o["object"] != "ExternalToolArtifact"]
    data["total_objects"] -= 1
    tampered = tmp_path / "state_machines.yaml"
    tampered.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    monkeypatch.setattr(gs, "STATE_MACHINES", tampered)

    problems = gs.drift_problems()
    assert problems, "删掉权威状态机后派生文件仍被判为一致，闸门失明"
    assert any("enum_registry" in p or "object_schemas" in p for p in problems), problems
