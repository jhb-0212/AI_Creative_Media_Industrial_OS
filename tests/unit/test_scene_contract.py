"""场次聚合根契约（Scene）的回归测试，兼作引用目标闸门的自证。

守的是四件事：

1. **字段边界**：Scene 只有身份、业务编号、归属与生命周期。场次名、地点、日夜、
   场次目标、进出状态、镜头集合、场次排序都不得出现在这里——它们各有落点
   （`ShotBreakdownDraft` 的场次层、`Shot.scene_id` 反查、`scene_code`）。多一个
   字段就多一个事实源，而且这个对象没有历史包袱，没有任何理由宽容。
2. **状态集**：`DRAFT` / `ACTIVE` / `ARCHIVED` 三个值必须与 `state_machines.yaml`、
   `enum_registry.yaml` 三处逐字一致；`ARCHIVED` 没有出边。裸 `string` 被明确拒绝，
   因为 C.3.1 要求未知枚举值必须拒绝，而裸 string 等于任何值都接受。
3. **引用可解析**：`Shot` / `ShotRevision` / `ShotBreakdownDraft` / `Scene` 声明的
   引用目标必须都是已定义对象。**这一条是本轮的核心**：Scene 曾长期没有 schema，
   而 `Shot.scene_id` 一直指向它，三闸门全绿——因为过去只校验 action 的
   resource_type，字段级引用从来没人在看。
4. **闸门真的会拦**：`reference_target_problems()` / `enum_registry_coverage_problems()`
   是纯函数，本文件直接注入悬空引用与缺失对象，验证它们返回问题而不是静默通过。

**诚实说明**：本文件不测试运行时。Scene 目前没有 ORM、没有迁移、没有 service、
没有 action，八条不变量里只有两条能由 schema 表达，其余全是 `RUNTIME_PENDING`。
断言「运行时尚未实现」是刻意的——它保证这条缺口不会被一句「契约已完成」悄悄抹掉。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import contract_check as cc  # noqa: E402

SCHEMA = json.loads(
    (ROOT / "contracts/schemas/object_schemas.schema.json").read_text(encoding="utf-8")
)
DEFS = SCHEMA["definitions"]
OBJECT = "Scene"
DEF = DEFS[OBJECT]
PROPS = DEF["properties"]

STATE_MACHINES = yaml.safe_load(
    (ROOT / "contracts/state_machines/state_machines.yaml").read_text(encoding="utf-8")
)
ENUMS = yaml.safe_load(
    (ROOT / "contracts/enums/enum_registry.yaml").read_text(encoding="utf-8")
)
GAPS = yaml.safe_load(
    (ROOT / "contracts/runtime/resource_schema_gaps.yaml").read_text(encoding="utf-8")
)

ENVELOPE = (
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
)
SCENE_STATES = {"DRAFT", "ACTIVE", "ARCHIVED"}


def _sm_entry(name: str) -> dict[str, Any]:
    return next(o for o in STATE_MACHINES["objects"] if o.get("object") == name)


def _enum_entry(name: str) -> dict[str, Any]:
    return next(o for o in ENUMS["objects"] if o.get("object") == name)


def _edges(name: str) -> set[tuple[str, str]]:
    edges: set[tuple[str, str]] = cc.parse_state_chain(
        str(_sm_entry(name).get("states", ""))
    )
    return edges


# --------------------------------------------------------------------------- #
# 1. 字段边界
# --------------------------------------------------------------------------- #
def test_scene_is_modeled() -> None:
    assert OBJECT in DEFS


def test_required_equals_the_whole_property_set() -> None:
    """14 个属性全部必填：每多一个可省字段，就多一条「填了也可能没填」的歧义。"""
    assert set(DEF["required"]) == set(PROPS)
    assert len(PROPS) == 14


def test_envelope_is_complete() -> None:
    """C.3.1 的十项信封缺一项，对象就不是权威领域对象。"""
    missing = [field for field in ENVELOPE if field not in PROPS]
    assert missing == []
    assert set(ENVELOPE) <= set(DEF["required"])


def test_ownership_keys_are_exactly_three() -> None:
    """归属只有 scene_id / episode_id / project_id 三列，多一列就是第四套事实。"""
    ownership = {"scene_id", "episode_id", "project_id"}
    assert ownership <= set(PROPS)
    for extra in ("series_id", "season_id", "tenant_id", "organization_id"):
        assert extra not in PROPS


def test_content_fields_are_excluded() -> None:
    """场次描述性内容已有落点（ShotBreakdownDraft 场次层），不在这里复制一份。"""
    for field in (
        "scene_name",
        "location",
        "time_of_day",
        "scene_goal",
        "core_event",
        "entry_state",
        "exit_state",
        "characters",
        "shots",
        "shot_ids",
        "scene_order",
        "constraint_pack_id",
    ):
        assert field not in PROPS, f"{field} 不该出现在 Scene 上"


def test_exclusions_explain_every_omitted_group() -> None:
    """排除项必须逐条写清「为什么不在」，否则下一代实现会以为是漏了。"""
    text = json.dumps(DEF["x-afs-exclusions"], ensure_ascii=False)
    for token in ("scene_name", "shots[]", "scene_order", "constraint_pack_id", "gate_result"):
        assert token in text, f"排除项没提到 {token}"


def test_additional_properties_is_closed() -> None:
    """新对象从第一天闭合：多塞一个字段在契约层就被拒绝，不靠文档承诺。"""
    assert DEF["additionalProperties"] is False


def test_scene_code_cannot_be_blank() -> None:
    """C.3.1 禁止用空字符串伪造值；minLength 是这条规则的唯一可执行形式。"""
    assert PROPS["scene_code"]["minLength"] == 1


# --------------------------------------------------------------------------- #
# 2. 状态集三处一致
# --------------------------------------------------------------------------- #
def test_status_enum_matches_state_machine() -> None:
    states = {a for a, _ in _edges(OBJECT)} | {b for _, b in _edges(OBJECT)}
    assert set(PROPS["status"]["enum"]) == states == SCENE_STATES


def test_status_enum_matches_enum_registry() -> None:
    assert set(_enum_entry(OBJECT)["states"]) == SCENE_STATES


def test_unknown_status_is_rejected() -> None:
    """这三类状态被明确排除：审核归 GateInstance、锁定归剧本/资产、进度归 Shot。"""
    for value in ("IN_REVIEW", "LOCKED", "IN_PRODUCTION", "DELIVERED", "REVIEW"):
        assert value not in PROPS["status"]["enum"], f"{value} 不该是场次状态"


def test_archived_is_terminal() -> None:
    assert [edge for edge in _edges(OBJECT) if edge[0] == "ARCHIVED"] == []


def test_declared_transitions_are_exactly_three() -> None:
    assert _edges(OBJECT) == {
        ("DRAFT", "ACTIVE"),
        ("ACTIVE", "ARCHIVED"),
        ("DRAFT", "ARCHIVED"),
    }


def test_mirrors_episode_lifecycle() -> None:
    """镜像不是巧合：同层级聚合共享同一形状，实现方只需学一次。

    若哪天有人给 Scene 加了一个 Episode 没有的状态，必须同时解释为什么同层级
    的两个聚合需要不同的生命周期——本用例就是那道追问。
    """
    assert _edges(OBJECT) == _edges("Episode")


def test_invariants_are_registered_and_partitioned() -> None:
    invariants = DEF["x-afs-invariants"]
    assert [item["id"] for item in invariants] == [f"SC-{i}" for i in range(1, 7)]
    for item in invariants:
        assert item["enforced_by"] in {"SCHEMA", "RUNTIME_PENDING"}
    schema_enforced = {item["id"] for item in invariants if item["enforced_by"] == "SCHEMA"}
    assert schema_enforced == {"SC-4", "SC-6"}, (
        "能由 schema 表达的只有「状态范围」与「字段集合闭合」；"
        f"实际标注: {sorted(schema_enforced)}"
    )


def test_scene_is_not_in_the_gap_ledger() -> None:
    """已建模却还挂在缺口台账上就是陈旧债务，闸门会报红，这里先拦一道。"""
    assert OBJECT not in (GAPS.get("gaps") or {})


# --------------------------------------------------------------------------- #
# 3. 引用目标可解析
# --------------------------------------------------------------------------- #
def test_every_declared_reference_resolves() -> None:
    assert cc.reference_target_problems(DEFS) == []


def test_scene_declares_its_own_identity_and_parents() -> None:
    assert DEF["x-afs-reference-targets"] == {
        "scene_id": "Scene",
        "project_id": "Project",
        "episode_id": "Episode",
    }


def test_shot_domain_points_at_scene() -> None:
    """这两个字段就是 Scene 必须存在的理由：它们指向一个曾不存在的对象。"""
    assert DEFS["Shot"]["x-afs-reference-targets"]["scene_id"] == "Scene"
    assert DEFS["ShotRevision"]["x-afs-reference-targets"]["scene_id"] == "Scene"


def test_dangling_reference_is_rejected() -> None:
    """悬空引用必须报红——这正是 Scene 缺失多年却全绿的那个漏洞。"""
    broken = {
        "Shot": {
            "type": "object",
            "properties": {"scene_id": {"type": "string"}},
            "x-afs-reference-targets": {"scene_id": "Scene"},
        }
    }
    problems = cc.reference_target_problems(broken)
    assert any("未定义对象 Scene" in p for p in problems), problems


def test_reference_to_undeclared_field_is_rejected() -> None:
    """声明腐化：字段改名后声明没跟上，悬空判定就会失效，所以字段本身也要校验。"""
    broken = {
        "Shot": {
            "type": "object",
            "properties": {"scene_id": {"type": "string"}},
            "x-afs-reference-targets": {"sceneId": "Shot"},
        }
    }
    problems = cc.reference_target_problems(broken)
    assert any("不存在字段" in p for p in problems), problems


def test_empty_reference_map_is_rejected() -> None:
    broken = {"Shot": {"type": "object", "properties": {}, "x-afs-reference-targets": {}}}
    assert cc.reference_target_problems(broken)


# --------------------------------------------------------------------------- #
# 4. 枚举覆盖闸门
# --------------------------------------------------------------------------- #
def test_enum_registry_covers_every_state_machine() -> None:
    assert cc.enum_registry_coverage_problems(
        STATE_MACHINES["objects"], ENUMS["objects"]
    ) == []


def test_enum_coverage_detects_missing_object() -> None:
    problems = cc.enum_registry_coverage_problems(
        [{"object": "Scene"}, {"object": "Ghost"}], [{"object": "Scene"}]
    )
    assert any("Ghost" in p for p in problems), problems


def test_registry_counts_match_their_lists() -> None:
    assert STATE_MACHINES["total_objects"] == len(STATE_MACHINES["objects"])
    assert ENUMS["object_count"] == len(ENUMS["objects"])


# --------------------------------------------------------------------------- #
# 5. 运行时缺口：断言「还没做」，防止被一句「契约完成」抹掉
# --------------------------------------------------------------------------- #
def test_scene_has_no_runtime_state_machine_yet() -> None:
    """Scene 尚未进入 _STATE_MACHINE_SOURCES（无领域实现）。

    这条一旦失败，说明运行时正在落地：那时必须同步补 PostgreSQL 集成测试
    （复合外键、唯一索引、终态不可复活），并把 SC-1/SC-2/SC-3/SC-5 从
    RUNTIME_PENDING 改判为 SCHEMA 或 RUNTIME_ENFORCED。
    """
    assert OBJECT not in cc._STATE_MACHINE_SOURCES


def test_all_runtime_gaps_are_declared_as_invariants() -> None:
    """没有运行时的规则必须至少留在不变量里，不能只存在于某个人的记忆里。"""
    runtime_pending = {
        item["statement"]
        for item in DEF["x-afs-invariants"]
        if item["enforced_by"] == "RUNTIME_PENDING"
    }
    assert len(runtime_pending) == 4
    joined = " ".join(runtime_pending)
    for token in ("复合外键", "scene_code", "ARCHIVED"):
        assert token in joined, f"运行时缺口没提到 {token}"
