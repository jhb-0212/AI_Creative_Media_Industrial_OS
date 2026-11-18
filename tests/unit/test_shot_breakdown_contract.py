"""拆镜草案对象契约（ShotBreakdownDraft）的回归测试。

这些用例守的不是「文件里有没有这个词」，而是几处容易在后续改动中悄悄退化的决定：

1. `required` 只能约束已声明字段（含嵌套层级）——手写几百行 JSON 时最容易漏掉。
2. 状态集三处一致：object schema 的 `status` 枚举、state_machines.yaml 的转移表、
   enum_registry.yaml 的权威状态值。三处任一被单独改动，用例立刻失败。
3. 每个终态都必须有对应的证据字段要求（CONFIRMED / RETURNED / DISCARDED）。
   新增第六个状态却忘了定义它的证据要求，就是「状态可到达、但可以空手进入」。
4. P0 分级表必须等于镜头条目的 `required`：分级表用来指导实现，一旦与契约脱节，
   实现就会按错的标准校验。
5. `SHOT.DRAFT.CREATE` 的资源类型必须解析到已定义对象，且不再回退到旧名 `ShotDraft`。
6. 缺口台账不得再挂 `ShotDraft`——补了 schema 仍挂债务是陈旧债务，闸门会报红，
   这里提前用单测钉住，避免有人「为了保险」把条目加回去。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import contract_check as cc  # noqa: E402

SCHEMA = json.loads((ROOT / "contracts/schemas/object_schemas.schema.json").read_text(encoding="utf-8"))
OBJECT = "ShotBreakdownDraft"
DEF = SCHEMA["definitions"][OBJECT]

STATE_MACHINES = yaml.safe_load(
    (ROOT / "contracts/state_machines/state_machines.yaml").read_text(encoding="utf-8")
)
ENUMS = yaml.safe_load(
    (ROOT / "contracts/enums/enum_registry.yaml").read_text(encoding="utf-8")
)
ACTIONS = yaml.safe_load(
    (ROOT / "contracts/action_registry/action_registry.yaml").read_text(encoding="utf-8")
)
GAPS = yaml.safe_load(
    (ROOT / "contracts/runtime/resource_schema_gaps.yaml").read_text(encoding="utf-8")
)

TERMINAL_STATES = {"CONFIRMED", "RETURNED", "DISCARDED"}


def _object_entries(entries, key: str):
    return next(item for item in entries if item.get(key) == OBJECT)


def _walk_object_schemas(node):
    """递归遍历所有内联对象定义（含数组 items），产出 (路径, 定义)。"""
    if isinstance(node, dict):
        if node.get("type") == "object" and isinstance(node.get("properties"), dict):
            yield node
        for value in node.values():
            yield from _walk_object_schemas(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_object_schemas(item)


def _shot_entry() -> dict:
    scene = DEF["properties"]["scene_breakdowns"]["items"]
    return scene["properties"]["shot_drafts"]["items"]


# --------------------------------------------------------------------------- #
# 结构完整性
# --------------------------------------------------------------------------- #
def test_object_registered_with_properties() -> None:
    assert DEF["type"] == "object"
    assert DEF["properties"], "空壳对象等于没定义"
    assert "shot_breakdown_draft_id" in DEF["properties"]


def test_required_fields_are_all_declared() -> None:
    """每一层 required 都必须落在同层的 properties 里（含嵌套对象与数组 items）。"""
    checked = 0
    for spec in _walk_object_schemas(DEF):
        properties = spec["properties"]
        for name in spec.get("required", []):
            assert name in properties, f"required 含未声明字段: {name}"
            checked += 1
    assert checked >= 40, f"嵌套 required 校验覆盖过少（{checked}），遍历逻辑可能失效"


def test_arrays_that_must_not_be_empty() -> None:
    """草案、节拍、镜头、节拍覆盖的镜头：任一为空都意味着「拆了个寂寞」。"""
    assert DEF["properties"]["scene_breakdowns"]["minItems"] == 1
    scene = DEF["properties"]["scene_breakdowns"]["items"]
    assert scene["properties"]["story_beats"]["minItems"] == 1
    assert scene["properties"]["shot_drafts"]["minItems"] == 1
    beat = scene["properties"]["story_beats"]["items"]
    assert beat["properties"]["covered_shot_refs"]["minItems"] == 1


def test_scene_entry_and_exit_state_are_mandatory() -> None:
    """场次起止状态是「拆镜不丢剧情状态」的唯一落点，不能退成可选。"""
    scene = DEF["properties"]["scene_breakdowns"]["items"]
    assert {"entry_state", "exit_state"} <= set(scene["required"])


# --------------------------------------------------------------------------- #
# 分级与必填的一致性
# --------------------------------------------------------------------------- #
def test_p0_matches_shot_entry_required() -> None:
    criticality = DEF["x-afs-criticality"]
    assert set(criticality["P0"]) == set(_shot_entry()["required"])

    declared = set(_shot_entry()["properties"])
    for level in ("P0", "P1", "P2", "P3"):
        unknown = set(criticality[level]) - declared
        assert not unknown, f"{level} 分级含未声明字段: {sorted(unknown)}"

    graded = set().union(*(set(criticality[level]) for level in ("P0", "P1", "P2", "P3")))
    assert graded == declared, f"字段未分级: {sorted(declared - graded)}"


# --------------------------------------------------------------------------- #
# 状态集：schema / 状态机 / 枚举注册表 三处一致
# --------------------------------------------------------------------------- #
def test_status_enum_matches_state_machine() -> None:
    schema_states = set(DEF["properties"]["status"]["enum"])

    entry = _object_entries(STATE_MACHINES["objects"], "object")
    edges = cc.parse_state_chain(str(entry["states"]))
    machine_states = {state for edge in edges for state in edge}

    assert schema_states == machine_states, (
        f"schema 状态集 {sorted(schema_states)} 与状态机 {sorted(machine_states)} 不一致"
    )


def test_status_enum_matches_enum_registry() -> None:
    schema_states = set(DEF["properties"]["status"]["enum"])
    entry = _object_entries(ENUMS["objects"], "object")
    assert schema_states == set(entry["states"])


def test_state_machine_transitions_are_exactly_as_designed() -> None:
    """五态五边：多一条或少一条都属于「实现时自创流程」。"""
    entry = _object_entries(STATE_MACHINES["objects"], "object")
    assert cc.parse_state_chain(str(entry["states"])) == {
        ("PENDING_CONFIRM", "IN_REVIEW"),
        ("IN_REVIEW", "CONFIRMED"),
        ("IN_REVIEW", "RETURNED"),
        ("PENDING_CONFIRM", "DISCARDED"),
        ("IN_REVIEW", "DISCARDED"),
    }


def test_each_terminal_state_requires_evidence() -> None:
    """终态必须带证据字段：确认要绑定 ShotRevision，退回要写原因，废弃要写原因。"""
    covered = {
        rule["if"]["properties"]["status"]["const"]: set(rule["then"]["required"])
        for rule in DEF["allOf"]
        if "status" in rule.get("if", {}).get("properties", {})
    }

    assert set(covered) == TERMINAL_STATES, (
        "每个终态都必须有证据要求，且非终态不得被证据规则误伤"
    )
    for state, required in covered.items():
        assert required, f"{state} 的证据要求不能为空"
        assert required <= set(DEF["properties"]), f"{state} 要求了未声明字段"


def test_version_chain_rules() -> None:
    """首版不得有前驱；非首版必须写明取代谁、改了什么。"""
    rules = [rule for rule in DEF["allOf"] if "draft_version" in rule.get("if", {}).get("properties", {})]
    assert len(rules) == 2, "版本链规则应有两条（首版 / 非首版）"

    first = next(rule for rule in rules if rule["if"]["properties"]["draft_version"].get("const") == 1)
    assert first["then"]["properties"]["supersedes_draft_ref"] == {"type": "null"}

    later = next(rule for rule in rules if "minimum" in rule["if"]["properties"]["draft_version"])
    assert {"supersedes_draft_ref", "change_summary"} <= set(later["then"]["required"])


# --------------------------------------------------------------------------- #
# 不变量与来源
# --------------------------------------------------------------------------- #
def test_invariants_are_declared_and_scoped() -> None:
    invariants = DEF["x-afs-invariants"]
    ids = [item["id"] for item in invariants]
    assert len(ids) == len(set(ids)), "不变量 id 重复"

    for item in invariants:
        assert item["statement"].strip()
        assert item["enforced_by"] in {"SCHEMA", "RUNTIME_PENDING"}

    assert any(item["enforced_by"] == "SCHEMA" for item in invariants)
    assert any(item["enforced_by"] == "RUNTIME_PENDING" for item in invariants), (
        "尚未运行的校验必须显式标为待运行时，不能默认当成已落地"
    )


def test_source_declares_owner_supplied_origin() -> None:
    """字段口径来自所有者设计而非正文，这一点必须在契约里写清楚。"""
    sources = " ".join(DEF["x-afs-sources"])
    assert "非基线正文原文" in sources
    assert "ADR-R16" in sources


# --------------------------------------------------------------------------- #
# 引用面
# --------------------------------------------------------------------------- #
def test_action_resource_type_resolves_to_defined_object() -> None:
    action = next(a for a in ACTIONS["actions"] if a["action_key"] == "SHOT.DRAFT.CREATE")
    assert action["resource_type"] == OBJECT
    assert action["resource_type"] in SCHEMA["definitions"]


def test_gap_ledger_no_longer_lists_the_gap() -> None:
    gaps = GAPS["gaps"]
    assert "ShotDraft" not in gaps, "ShotDraft 已有 schema（改名为 ShotBreakdownDraft），陈旧债务必须删除"
    assert OBJECT not in gaps
    assert "ShotRevision" in gaps, "ShotRevision 仍无 schema，缺口不能被顺手删掉"


def test_coverage_ledger_counts_match_definitions() -> None:
    coverage = SCHEMA["x-afs-p0-coverage"]
    assert coverage["total"] == len(SCHEMA["definitions"])
    assert len(coverage["modeled"]) == coverage["total"]
    assert OBJECT in coverage["modeled"]
