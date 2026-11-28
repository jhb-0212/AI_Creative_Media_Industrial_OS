"""维度字段值集闸门的负向测试。

判据不是「当前契约跑一遍是绿的」，而是**注入坏状态后必须精确报 FAILED**：
值集与 Schema 脱节、陈旧待裁定债务、status_split 权威字段隐形缺口、
转移边/guard 引用不存在的字面值。每个用例只注入一个变异点，避免一个守卫
先抛错掩盖另一个（TECH-04 的假阳性教训）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import contract_check as cc  # noqa: E402
import gen_status_contracts as gs  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
FIELD_VALUE_SETS = ROOT / "contracts" / "enums" / "field_value_sets.yaml"
SCHEMA_PATH = ROOT / "contracts" / "schemas" / "object_schemas.schema.json"
LAYERS_PATH = ROOT / "contracts" / "enums" / "status_layers.yaml"


def _definitions() -> dict[str, Any]:
    data = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    return cast("dict[str, Any]", data["definitions"])


def _layers() -> dict[str, Any]:
    return cast("dict[str, Any]", yaml.safe_load(LAYERS_PATH.read_text(encoding="utf-8")))


def _dump(data: dict[str, Any], path: Path) -> Path:
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


def _sets() -> dict[str, Any]:
    return cast("dict[str, Any]", yaml.safe_load(FIELD_VALUE_SETS.read_text(encoding="utf-8")))


def _authority_entries() -> dict[str, set[str]]:
    return {
        "ExternalToolAdapterManifest": {"lifecycle_status"},
        "ModelInvocation": {"submission_status", "result_status", "billing_status"},
        "QCIssue": {"human_decision", "disposition"},
    }


# --------------------------------------------------------------------------- #
# 正向：权威源、Schema、债务台账三者当前一致
# --------------------------------------------------------------------------- #
def test_declared_value_sets_land_in_schema() -> None:
    problems = cc.field_value_set_problems(
        FIELD_VALUE_SETS, _definitions(), _layers(), _authority_entries()
    )
    assert problems == [], problems


def test_schema_enums_match_declared_values() -> None:
    definitions = _definitions()
    for (obj, field), values in gs.field_value_sets().items():
        assert set(definitions[obj]["properties"][field]["enum"]) == set(values)


def test_status_field_is_not_a_dimension_entry() -> None:
    """信封 status 的权威源只有 state_machines，两套事实源重叠即契约损坏。"""
    with pytest.raises(ValueError, match="信封 status"):
        gs.field_value_sets_from_data(
            {"fields": [{"object": "Scene", "field": "status", "values": ["DRAFT"]}]}
        )


# --------------------------------------------------------------------------- #
# 负向：注入坏状态，判据必须报 FAILED
# --------------------------------------------------------------------------- #
def test_unknown_value_in_declaration_is_reported(tmp_path: Path) -> None:
    data = _sets()
    data["fields"][0]["values"].append("GHOST_STATE")
    path = _dump(data, tmp_path / "field_value_sets.yaml")
    problems = cc.field_value_set_problems(path, _definitions(), _layers(), _authority_entries())
    assert any("GHOST_STATE" in p for p in problems), problems


def test_schema_enum_drift_is_reported(tmp_path: Path) -> None:
    definitions = json.loads(json.dumps(_definitions()))
    definitions["ExternalToolAdapterManifest"]["properties"]["lifecycle_status"]["enum"].remove(
        "REVOKED"
    )
    problems = cc.field_value_set_problems(
        FIELD_VALUE_SETS, definitions, _layers(), _authority_entries()
    )
    assert any("REVOKED" in p and "不一致" in p for p in problems), problems


def test_bare_string_dimension_field_is_reported() -> None:
    definitions = json.loads(json.dumps(_definitions()))
    definitions["QCIssue"]["properties"]["human_decision"] = {"type": "string"}
    problems = cc.field_value_set_problems(
        FIELD_VALUE_SETS, definitions, _layers(), _authority_entries()
    )
    assert any("human_decision" in p and "不一致" in p for p in problems), problems


def test_stale_pending_entry_is_reported() -> None:
    layers = json.loads(json.dumps(_layers()))
    layers["non_lifecycle_dimensions"].append(
        {
            "object": "QCIssue",
            "dimension": "ghost_duplicate",
            "values": [],
            "value_set_status": "pending_owner_ruling",
            "note": "人为注入陈旧债务",
            "status_field": "human_decision",
        }
    )
    problems = cc.field_value_set_problems(
        FIELD_VALUE_SETS, _definitions(), layers, _authority_entries()
    )
    assert any("陈旧债务" in p and "human_decision" in p for p in problems), problems


def test_hidden_gap_without_value_set_or_pending_entry_is_reported() -> None:
    layers = json.loads(json.dumps(_layers()))
    layers["non_lifecycle_dimensions"] = [
        entry
        for entry in layers["non_lifecycle_dimensions"]
        if not (
            entry.get("object") == "QCIssue" and "disposition" in str(entry.get("status_field", ""))
        )
    ]
    problems = cc.field_value_set_problems(
        FIELD_VALUE_SETS, _definitions(), layers, _authority_entries()
    )
    assert any("disposition" in p and "缺口不能隐形" in p for p in problems), problems


def test_transition_to_unknown_value_is_reported(tmp_path: Path) -> None:
    data = _sets()
    data["fields"][0]["transitions"].append({"from": "ACTIVE", "to": "GHOST_STATE"})
    path = _dump(data, tmp_path / "field_value_sets.yaml")
    problems = cc.field_value_set_problems(path, _definitions(), _layers(), _authority_entries())
    assert any("GHOST_STATE" in p for p in problems), problems


def test_guard_to_unknown_field_is_reported(tmp_path: Path) -> None:
    data = _sets()
    data["fields"][0]["guards"][0]["require"].append("ghost_ref")
    path = _dump(data, tmp_path / "field_value_sets.yaml")
    problems = cc.field_value_set_problems(path, _definitions(), _layers(), _authority_entries())
    assert any("ghost_ref" in p for p in problems), problems


def test_generator_detects_schema_field_enum_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """字节级派生漂移必须被 --check 的同一实现接住，不能只靠语义判据。"""
    raw = SCHEMA_PATH.read_text(encoding="utf-8")
    tampered = raw.replace('"RESULT_UNKNOWN"', '"RESULT_GHOST"', 1)
    path = tmp_path / "object_schemas.schema.json"
    path.write_text(tampered, encoding="utf-8")
    monkeypatch.setattr(gs, "SCHEMA", path)

    problems = gs.drift_problems()
    assert any("字段 enum 与权威源不一致" in p for p in problems), problems
