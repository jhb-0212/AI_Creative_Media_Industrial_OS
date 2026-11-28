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

**诚实说明（2026-11-20 更新）**：A13 / ADR-R28 已把 Scene 的运行时补齐——ORM、
迁移、service、action 与状态机全部就位，SC-1 / SC-2 / SC-3 / SC-5 由
`RUNTIME_PENDING` 改判为 `RUNTIME_ENFORCED`。因此本节从「断言还没做」翻转为
「断言已做且定级没有虚高」：反向的谎（实现了但定级不更新）与正向的谎
（定级说强制但运行时没有）都要能被拦住。
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
ENUMS = yaml.safe_load((ROOT / "contracts/enums/enum_registry.yaml").read_text(encoding="utf-8"))
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
    """已授权的转移边（v2 结构化：直接读 transitions，不再解析文本）。"""
    return cc.contract_edges(_sm_entry(name))


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
    """允许状态集合与转移边是两件事：这里断言的是前者（三个状态全部合法）。"""
    states = set(_sm_entry(OBJECT)["allowed_states"])
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
    """不变量必须齐备，且定级取自三分词汇表。

    词汇表在 A13 增加 `RUNTIME_ENFORCED`：此前只有「schema 能表达」与「运行时
    还没做」两档，把「已经在运行时强制」挤进了 PENDING，等于交付完成后定级必须
    说谎。三档分别对应：契约层拒绝、运行时拒绝、尚未落地。
    """
    invariants = DEF["x-afs-invariants"]
    assert [item["id"] for item in invariants] == [f"SC-{i}" for i in range(1, 7)]
    for item in invariants:
        assert item["enforced_by"] in cc.INVARIANT_LEVELS
    schema_enforced = {item["id"] for item in invariants if item["enforced_by"] == "SCHEMA"}
    assert schema_enforced == {"SC-4", "SC-6"}, (
        f"能由 schema 表达的只有「状态范围」与「字段集合闭合」；实际标注: {sorted(schema_enforced)}"
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
    """注册表必须是状态机的等值投影：对象集与每对象状态集都相等（v2 收紧）。"""
    sm_states = cc.state_machine_states(STATE_MACHINES["objects"])
    assert cc.enum_registry_problems(ENUMS["objects"], sm_states) == []


def test_enum_coverage_detects_missing_object() -> None:
    problems = cc.enum_registry_problems(
        [{"object": "Scene", "states": ["DRAFT"]}],
        {"Scene": {"DRAFT"}, "Ghost": {"DRAFT"}},
    )
    assert any("Ghost" in p for p in problems), problems


def test_enum_coverage_detects_extra_state() -> None:
    """注册表多出一个权威集合里没有的值（历史上的 PG0 / PMS 就是这一类）。"""
    problems = cc.enum_registry_problems(
        [{"object": "Scene", "states": ["DRAFT", "PG0"]}], {"Scene": {"DRAFT"}}
    )
    assert any("PG0" in p for p in problems), problems


def test_registry_counts_match_their_lists() -> None:
    assert STATE_MACHINES["total_objects"] == len(STATE_MACHINES["objects"])
    assert ENUMS["object_count"] == len(ENUMS["objects"])


# --------------------------------------------------------------------------- #
# 5. 运行时落地：断言「已落地」，并拦住虚高的定级
# --------------------------------------------------------------------------- #
# A13 交付时逐条核过的定级账：每条都必须来自「运行时真的会拒绝」，而不是
# 「代码里有相关字段」。这张表的作用是防止实现回退后定级仍留在 ENFORCED。
_ENFORCED_AFTER_A13 = {
    "SC-1": "领域层构造校验 + ck_scenes_identity_single",
    "SC-2": "复合外键 fk_scenes_episode（(project, episode) → episodes）",
    "SC-3": "uq_scene_code_per_project（项目内唯一）+ 服务层冲突解释",
    "SC-5": "ARCHIVED 无出边 + SceneService 归档前置条件（无 IN_PRODUCTION 镜头）",
}


def test_scene_has_runtime_state_machine() -> None:
    """Scene 已进入 `_STATE_MACHINE_SOURCES`：运行时转移表与契约逐边相等。

    刻意复用闸门自己的 ``_load_impl_transitions`` / ``contract_edges``，而不是在
    测试里再写一遍反射逻辑——否则「闸门认为一致」与「测试认为一致」会变成两个
    判据，而它们迟早会给出不同答案。
    """
    assert OBJECT in cc._STATE_MACHINE_SOURCES
    module_path, var_name = cc._STATE_MACHINE_SOURCES[OBJECT]
    entry = next(o for o in STATE_MACHINES["objects"] if o.get("object") == OBJECT)
    assert cc._load_impl_transitions(module_path, var_name) == cc.contract_edges(entry)


def test_scene_invariants_are_no_longer_runtime_pending() -> None:
    """A13 交付的 4 条不变量必须已改判，且 Scene 不再有任何 PENDING 遗留。

    为什么先钉死具体条目再断言整体为空：只断言「空集」可以被「整体删掉不变量」
    满足，先确认这 4 条仍在且已定级，才排除「删掉即通过」这条捷径。
    """
    by_id = {item["id"]: item for item in DEF["x-afs-invariants"]}
    for invariant_id in _ENFORCED_AFTER_A13:
        assert invariant_id in by_id, f"不变量 {invariant_id} 被删除了"
        assert by_id[invariant_id]["enforced_by"] == "RUNTIME_ENFORCED", (
            f"{invariant_id} 应已由运行时不变量强制"
        )

    pending = [
        item["id"] for item in DEF["x-afs-invariants"] if item["enforced_by"] == "RUNTIME_PENDING"
    ]
    assert pending == [], f"Scene 仍有未落地的不变量：{pending}"


def test_runtime_enforced_claim_requires_runtime() -> None:
    """定级不得虚高：声称 `RUNTIME_ENFORCED` 的对象必须已有运行时状态机。

    这是原 `len(runtime_pending) == 4` 的替代。那种写法把「数量」当判据，一旦
    某个对象落地，修法就变成去改数字——判据沦为易被篡改的常数，而且改数字的人
    不需要理解任何一条不变量。换成「定级与运行时是否存在」的结构性约束后，
    判据不会随交付进度失效，也不会奖励「把断言改宽」。
    """
    allowed = cc.INVARIANT_LEVELS
    for obj_name, spec in DEFS.items():
        for item in spec.get("x-afs-invariants", []):
            assert item["enforced_by"] in allowed, (
                f"{obj_name}.{item['id']} 的 enforced_by 取值非法：{item['enforced_by']}"
            )
            assert item["statement"].strip(), f"{obj_name}.{item['id']} 没有 statement"
            if item["enforced_by"] == "RUNTIME_ENFORCED":
                assert obj_name in cc._STATE_MACHINE_SOURCES, (
                    f"{obj_name}.{item['id']} 声称由运行时强制，"
                    f"但 {obj_name} 没有运行时状态机——定级虚高"
                )
