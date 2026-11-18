"""镜头内容修订对象契约（ShotRevision）的回归测试。

守的是几处「不写用例就会悄悄退化」的决定：

1. 身份与引用：`ShotSpec.shot_revision_id` 是必填项，`ShotRevision` 必须真的存在，
   否则草案确认产出的引用会退回「一头有契约、一头是字符串」。
2. 状态集三处一致（schema / 状态机 / 枚举注册表），且只允许 `ACTIVE→SUPERSEDED` 一条边：
   正文没有给本对象的状态集，多一条边就是实现时自创流程。
3. 版本链的条件必填：首版无前驱、非首版必须写明取代谁与改了什么；这两个条件写反，
   版本链就断了（或变成「任何修订都必须有前驱」的死锁）。
4. 约束适用性的双向规则：`BOUND` 必须给出 `constraint_pack_id`，`NOT_APPLICABLE` 必须为空。
   只写前一条，会允许「标了不适用却还挂着约束包」的自相矛盾记录。
5. 越权字段的负向清单：`locked_dimensions` / `prompt` / `shot_code` 等不得出现在本对象里。
   这类字段一旦被加进来，锁定维度与提示词就会出现第二个事实源。
6. 版本冲突拒绝不能被简化掉：`entity_version` 必须在必填里，它是乐观锁的载体。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import contract_check as cc  # noqa: E402

SCHEMA = json.loads(
    (ROOT / "contracts/schemas/object_schemas.schema.json").read_text(encoding="utf-8")
)
OBJECT = "ShotRevision"
DEF = SCHEMA["definitions"][OBJECT]
PROPS = DEF["properties"]

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

ENVELOPE = {
    "entity_id",
    "entity_type",
    "schema_version",
    "entity_version",
    "status",
    "owner_module",
    "created_at",
    "created_by",
    "updated_at",
    "audit_ref",
}


def _entry(entries, key: str):
    return next(item for item in entries if item.get(key) == OBJECT)


def _rule_for(field: str) -> dict:
    """取出以某个字段为条件的 if/then 规则。"""
    rules = [
        rule
        for rule in DEF["allOf"]
        if field in rule.get("if", {}).get("properties", {})
    ]
    assert rules, f"缺少以 {field} 为条件的规则"
    return rules


# --------------------------------------------------------------------------- #
# 结构完整性
# --------------------------------------------------------------------------- #
def test_object_is_not_an_empty_shell() -> None:
    assert DEF["type"] == "object"
    assert PROPS, "空壳对象等于没定义"
    assert DEF["description"].strip(), "对象必须有说明，否则实现者只能猜语义"


def test_required_fields_are_declared() -> None:
    missing = [name for name in DEF["required"] if name not in PROPS]
    assert not missing, f"required 含未声明字段: {missing}"


def test_envelope_is_complete() -> None:
    assert set(PROPS) >= ENVELOPE, f"通用信封缺字段: {sorted(ENVELOPE - set(PROPS))}"
    assert set(DEF["required"]) >= ENVELOPE, "信封字段必须全部必填"


def test_identity_is_immutable_by_contract() -> None:
    """标识类字段必须存在且写明不可复用，否则版本会与对象身份混在一起。"""
    for name in ("shot_revision_id", "shot_id", "project_id", "episode_id", "scene_id"):
        assert name in DEF["required"], f"{name} 必须必填"
    assert "不得复用" in PROPS["shot_revision_id"]["description"]
    assert "不得复用" in PROPS["shot_id"]["description"]


def test_optimistic_lock_carrier_is_required() -> None:
    """31.6 要求版本冲突必须拒绝，载体是 entity_version；缺了它只能后写覆盖。"""
    assert "entity_version" in DEF["required"]
    assert "冲突" in PROPS["entity_version"]["description"]


# --------------------------------------------------------------------------- #
# 状态集：schema / 状态机 / 枚举注册表 三处一致
# --------------------------------------------------------------------------- #
def test_status_enum_matches_state_machine() -> None:
    schema_states = set(PROPS["status"]["enum"])
    edges = cc.parse_state_chain(str(_entry(STATE_MACHINES["objects"], "object")["states"]))
    machine_states = {state for edge in edges for state in edge}
    assert schema_states == machine_states, (
        f"schema 状态集 {sorted(schema_states)} 与状态机 {sorted(machine_states)} 不一致"
    )


def test_status_enum_matches_enum_registry() -> None:
    schema_states = set(PROPS["status"]["enum"])
    assert schema_states == set(_entry(ENUMS["objects"], "object")["states"])


def test_state_machine_has_exactly_one_edge() -> None:
    """正文没给本对象的状态集，只允许「有效 → 被取代」这一条边。"""
    edges = cc.parse_state_chain(str(_entry(STATE_MACHINES["objects"], "object")["states"]))
    assert edges == {("ACTIVE", "SUPERSEDED")}, f"出现了自创转移: {sorted(edges)}"


def test_terminal_state_is_not_resurrected() -> None:
    """SUPERSEDED 是终态：状态机里不得出现从它出发的边。"""
    edges = cc.parse_state_chain(str(_entry(STATE_MACHINES["objects"], "object")["states"]))
    assert not any(source == "SUPERSEDED" for source, _ in edges)


# --------------------------------------------------------------------------- #
# 条件规则
# --------------------------------------------------------------------------- #
def test_version_chain_rules() -> None:
    rules = _rule_for("revision_no")
    assert len(rules) == 2, "版本链规则应有两条（首版 / 非首版）"

    first = next(rule for rule in rules if rule["if"]["properties"]["revision_no"].get("const") == 1)
    assert first["then"]["properties"]["supersedes_shot_revision_id"] == {"type": "null"}

    later = next(rule for rule in rules if "minimum" in rule["if"]["properties"]["revision_no"])
    assert {"supersedes_shot_revision_id", "change_summary"} <= set(later["then"]["required"])


def test_constraint_applicability_rules_are_bidirectional() -> None:
    """BOUND 必须给约束包；NOT_APPLICABLE 必须留空。只写一半会留下自相矛盾的记录。"""
    rules = {
        rule["if"]["properties"]["constraint_pack_applicability"]["const"]: rule
        for rule in _rule_for("constraint_pack_applicability")
    }
    assert set(rules) == {"BOUND", "NOT_APPLICABLE"}, "两种取值都必须有规则"
    assert "constraint_pack_id" in rules["BOUND"]["then"]["required"]
    assert rules["NOT_APPLICABLE"]["then"]["properties"]["constraint_pack_id"] == {"type": "null"}


def test_draft_confirm_requires_source_draft() -> None:
    """草案确认产生的修订必须指回草案版本，否则「确认即绑定」只能单向追。"""
    rules = _rule_for("change_origin")
    rule = next(
        item for item in rules if item["if"]["properties"]["change_origin"].get("const") == "BREAKDOWN_CONFIRM"
    )
    assert "source_draft_ref" in rule["then"]["required"]


# --------------------------------------------------------------------------- #
# 越权字段的负向清单
# --------------------------------------------------------------------------- #
def test_no_field_that_belongs_elsewhere() -> None:
    """锁定维度、提示词、业务短编号、生产进度都不得出现在修订里。"""
    forbidden = {
        "locked_dimensions",
        "prompt",
        "negative_prompt",
        "dialogue",
        "shot_code",
        "production_status",
    }
    leaked = forbidden & set(PROPS)
    assert not leaked, f"这些字段属于其他对象（ConstraintPack/ShotSpec/Shot/剧本），不得进修订: {sorted(leaked)}"


def test_exclusions_are_documented() -> None:
    """负向清单不能只靠「没写」，必须写明为什么没写，否则下次会有人补回来。"""
    documented = {item["field"] for item in DEF["x-afs-exclusions"]}
    for expected in ("shot_code", "locked_dimensions", "prompt", "dialogue"):
        assert any(expected in field for field in documented), f"{expected} 的排除理由未记录"
    for item in DEF["x-afs-exclusions"]:
        assert item["why"].strip()


# --------------------------------------------------------------------------- #
# 枚举收窄与可追溯
# --------------------------------------------------------------------------- #
def test_quality_profile_is_pinned_to_unified_baseline() -> None:
    """4.1：成片镜头绑定 UNIFIED_HIGH_QUALITY；放开枚举等于允许用白模标准衡量成片。"""
    assert PROPS["quality_profile"]["enum"] == ["UNIFIED_HIGH_QUALITY"]


def test_creative_autonomy_levels_match_text() -> None:
    assert PROPS["creative_autonomy"]["enum"] == ["L1", "L2", "L3", "L4"]


def test_risk_levels_match_text() -> None:
    assert PROPS["risk"]["properties"]["level"]["enum"] == [
        "LOW",
        "MEDIUM",
        "HIGH",
        "CRITICAL",
    ]


def test_intent_is_pinned_by_version() -> None:
    """8.8：引用固定 intent_id + intent_version，不能用「最新意图」代替。"""
    assert "intent_ref" in DEF["required"]
    assert set(PROPS["intent_ref"]["required"]) == {"intent_id", "intent_version"}


def test_script_anchors_cannot_be_empty() -> None:
    assert "script_anchors" in DEF["required"]
    assert PROPS["script_anchors"]["minItems"] == 1
    assert "basis_script_revision_id" in DEF["required"], "没有剧本版本，锚点无从界定范围"


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

    schema_enforced = {item["id"] for item in invariants if item["enforced_by"] == "SCHEMA"}
    assert schema_enforced == {"SR-4", "SR-6"}, (
        "能由 schema 表达的只有版本链闭合与越权字段排除；"
        f"实际标注: {sorted(schema_enforced)}"
    )
    assert any(item["enforced_by"] == "RUNTIME_PENDING" for item in invariants), (
        "尚未运行的校验必须显式标为待运行时，不能默认当成已落地"
    )


def test_sources_cite_the_text_not_just_the_adr() -> None:
    sources = " ".join(DEF["x-afs-sources"])
    for section in ("8.1", "8.2", "8.3", "8.8", "19.3", "31.6"):
        assert section in sources, f"缺少正文依据 {section}"
    assert "ADR-R17" in sources


# --------------------------------------------------------------------------- #
# 引用面
# --------------------------------------------------------------------------- #
def test_action_resource_type_resolves_to_defined_object() -> None:
    """首次确认原子创建 Shot + 首个 ShotRevision，两个对象都必须真实存在。

    ADR-R18 把资源类型从单值 ShotRevision 扩为组合类型：只声明修订的话，
    同事务新建的 Shot 即使缺必填字段也能通过闸门。
    """
    action = next(a for a in ACTIONS["actions"] if a["action_key"] == "SHOT.REVISION.CONFIRM")
    parts = {part.strip() for part in action["resource_type"].split("/")}
    assert parts == {"Shot", "ShotRevision"}
    assert parts <= set(SCHEMA["definitions"])


def test_shot_spec_reference_is_no_longer_dangling() -> None:
    """ShotSpec 的 shot_revision_id 是必填项，它必须指向一个真实存在的对象。"""
    shot_spec = SCHEMA["definitions"]["ShotSpec"]
    assert "shot_revision_id" in shot_spec["required"]
    assert "shot_revision_id" in SCHEMA["definitions"][OBJECT]["properties"]


def test_gap_ledger_entry_is_removed() -> None:
    assert OBJECT not in GAPS["gaps"], "补了 schema 仍挂债务是陈旧债务，闸门会报红"


def test_coverage_ledger_counts_match_definitions() -> None:
    coverage = SCHEMA["x-afs-p0-coverage"]
    assert coverage["total"] == len(SCHEMA["definitions"])
    assert len(coverage["modeled"]) == coverage["total"]
    assert OBJECT in coverage["modeled"]
