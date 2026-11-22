"""稳定逻辑镜头对象契约（Shot）的回归测试。

守的是「独立建模 Shot」这一决定容易被悄悄稀释的几处：

1. **状态集边界**：五态一个不多、一个不少。加 `IN_REVIEW` 就把审核事实搬进镜头，
   加 `REWORK` 就让返工语义永久含糊，加 `DELIVERED` 就会出现「声称已交付、拿不出
   Gate 4 证据」的状态。
2. **转移方向**：`CANCELLED` 没有出边；`NOT_STARTED` 不能直接跳 `PRODUCTION_COMPLETE`
   （跳过生产等于伪造进度）；`PRODUCTION_COMPLETE→IN_PRODUCTION` 必须有返工依据。
3. **字段边界**：内容、规格、约束、Gate 结论都不得出现在 Shot 上；`episode_id` 不得
   出现——归属链只有 `scene_id → Scene → Episode → Project` 一条，多存一份就是漂移源。
4. **状态证据双向**：BLOCKED 必须有原因、其余状态必须为空；CANCELLED 必须有授权记录
   与时间、其余状态必须为空。只写单向会允许「标了不适用却还挂着证据」的自相矛盾记录。
5. **守卫不是注释**：`guards` 引用的字段必须在 schema 里真实存在、转移必须是已登记的
   边。`guard_problems()` 是纯函数，本文件直接注入坏守卫验证它真的会拦。

**关于校验口径的诚实说明**：本轮 CI 没有 JSON Schema 引擎（已在残余风险里登记），
所以 `_violations()` 是一个只覆盖本契约用到的关键字的极小实现（type / required /
enum / allOf-if-then），不是通用 draft-07 校验器。它足以验证条件必填的正反向，
但不能代替真正的 schema 校验器；缺引擎这件事不该被这些用例掩盖。

跨 Scene 归属、重复 shot_code、非法状态转移、终态复活这四条**没有运行时**就无法真正
执行，本文件的做法是：断言它们被显式登记为 RUNTIME_PENDING 不变量、且状态机里有对应
的边/终态声明——守的是「缺口不会被静默遗忘」，而不是假装测了运行时。
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
OBJECT = "Shot"
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

# 所有者 2026-11-18 裁定的五态
OWNER_STATES = {
    "NOT_STARTED",
    "IN_PRODUCTION",
    "BLOCKED",
    "PRODUCTION_COMPLETE",
    "CANCELLED",
}

# 合法的基础实例（用于 payload 级负向用例）
BASE = {
    "shot_id": "sh_01J",
    "scene_id": "sc_01J",
    "project_id": "prj_01J",
    "shot_code": "EP001-SC003-SH012",
    "shot_revision_id": "shr_01J",
    "status": "NOT_STARTED",
    "entity_id": "sh_01J",
    "entity_type": "Shot",
    "schema_version": 1,
    "entity_version": 1,
    "owner_module": "shot_domain",
    "created_at": "2026-11-18T00:00:00Z",
    "created_by": "director@example",
    "updated_at": "2026-11-18T00:00:00Z",
    "audit_ref": None,
    "blocked_reason": None,
    "rework_basis_ref": None,
    "cancellation_ref": None,
    "cancelled_at": None,
}


def _entry(entries, key: str):
    return next(item for item in entries if item.get(key) == OBJECT)


SM_ENTRY = _entry(STATE_MACHINES["objects"], "object")
ENUM_ENTRY = _entry(ENUMS["objects"], "object")
EDGES = cc.contract_edges(SM_ENTRY)


# --------------------------------------------------------------------------- #
# 极小 schema 校验器：只覆盖本契约用到的关键字
# --------------------------------------------------------------------------- #
def _type_ok(value, expected: str) -> bool:
    if expected == "null":
        return value is None
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    return True


def _matches_condition(cond: dict, instance: dict) -> bool:
    """draft-07 语义：``properties`` 不约束缺失的键。"""
    for key, spec in cond.items():
        if key not in instance:
            continue
        value = instance[key]
        if "enum" in spec and value not in spec["enum"]:
            return False
        if "const" in spec and value != spec["const"]:
            return False
        if "minimum" in spec and (
            not isinstance(value, int) or value < spec["minimum"]
        ):
            return False
    return True


def _check_property(name: str, spec: dict, value, out: list[str]) -> None:
    declared = spec.get("type")
    if declared is not None:
        allowed = declared if isinstance(declared, list) else [declared]
        if not any(_type_ok(value, t) for t in allowed):
            out.append(f"{name} 类型非法: {value!r}")
            return
    if "enum" in spec and value not in spec["enum"]:
        out.append(f"{name} 不在枚举内: {value!r}")
    minimum = spec.get("minLength")
    if minimum is not None and isinstance(value, str) and len(value) < minimum:
        out.append(f"{name} 长度不足 {minimum}: {value!r}")


def _violations(definition: dict, instance: dict) -> list[str]:
    out: list[str] = []
    for name in definition.get("required", []):
        if name not in instance:
            out.append(f"缺必填 {name}")
    for name, spec in definition.get("properties", {}).items():
        if name in instance:
            _check_property(name, spec, instance[name], out)
    for rule in definition.get("allOf", []):
        if not _matches_condition(rule.get("if", {}).get("properties", {}), instance):
            continue
        then = rule.get("then", {})
        for name in then.get("required", []):
            if name not in instance:
                out.append(f"条件必填缺 {name}")
        for name, spec in then.get("properties", {}).items():
            if name in instance:
                _check_property(name, spec, instance[name], out)
    return out


def _shot(**overrides):
    payload = dict(BASE)
    payload.update(overrides)
    return payload


def _guard(transition: tuple[str, str]) -> dict:
    guards = SM_ENTRY.get("guards") or []
    return next(
        g
        for g in guards
        if (g["transition"]["from"], g["transition"]["to"]) == transition
    )


# --------------------------------------------------------------------------- #
# 1. 状态集边界
# --------------------------------------------------------------------------- #
def test_status_enum_is_exactly_the_five_owner_states() -> None:
    assert set(PROPS["status"]["enum"]) == OWNER_STATES


def test_gate_and_rework_states_must_not_enter_shot_status() -> None:
    """审核、返工、交付各有归属；混进 Shot.status 就是第二份事实。"""
    enum = set(PROPS["status"]["enum"])
    for forbidden in ("IN_REVIEW", "REWORK", "DELIVERED", "APPROVED", "PUBLISHED"):
        assert forbidden not in enum, f"{forbidden} 不属于镜头生产进度"


def test_status_is_declared_once_and_required() -> None:
    assert "status" in DEF["required"]
    # 信封的 status 就是业务 status，不允许分成两个字段
    assert sum(1 for name in PROPS if name.endswith("status")) == 1


def test_three_contract_sources_agree_on_state_set() -> None:
    """schema / 状态机 / 枚举注册表三处必须给同一套状态。"""
    assert set(SM_ENTRY["allowed_states"]) == OWNER_STATES
    assert set(ENUM_ENTRY["states"]) == OWNER_STATES


# --------------------------------------------------------------------------- #
# 2. 转移方向
# --------------------------------------------------------------------------- #
def test_transition_edges_match_owner_design_exactly() -> None:
    expected = {
        ("NOT_STARTED", "IN_PRODUCTION"),
        ("NOT_STARTED", "BLOCKED"),
        ("NOT_STARTED", "CANCELLED"),
        ("IN_PRODUCTION", "BLOCKED"),
        ("IN_PRODUCTION", "PRODUCTION_COMPLETE"),
        ("IN_PRODUCTION", "CANCELLED"),
        ("BLOCKED", "IN_PRODUCTION"),
        ("BLOCKED", "NOT_STARTED"),
        ("BLOCKED", "CANCELLED"),
        ("PRODUCTION_COMPLETE", "IN_PRODUCTION"),
    }
    assert expected == EDGES, f"多出 {EDGES - expected} / 缺少 {expected - EDGES}"


def test_cancelled_is_terminal() -> None:
    outgoing = {dst for src, dst in EDGES if src == "CANCELLED"}
    assert not outgoing, f"CANCELLED 出现出边 {outgoing}：终态可以原地复活了"


def test_progress_cannot_skip_production() -> None:
    """不能从「未开始」或「阻塞」直接宣称「生产完成」——那等于伪造进度。"""
    forbidden = {
        ("NOT_STARTED", "PRODUCTION_COMPLETE"),
        ("BLOCKED", "PRODUCTION_COMPLETE"),
        ("CANCELLED", "NOT_STARTED"),
        ("CANCELLED", "IN_PRODUCTION"),
    }
    assert not (forbidden & EDGES), f"存在跳步转移 {forbidden & EDGES}"


def test_rework_edge_exists_and_carries_basis_guard() -> None:
    assert ("PRODUCTION_COMPLETE", "IN_PRODUCTION") in EDGES
    guard = _guard(("PRODUCTION_COMPLETE", "IN_PRODUCTION"))
    assert guard["fields"] == ["rework_basis_ref"], (
        "无返工依据的回退必须被守卫拦住，否则等于绕过验收"
    )


def test_every_blocked_and_cancelled_edge_has_a_guard() -> None:
    """所有进入 BLOCKED / CANCELLED 的边都必须要求证据。"""
    declared = {
        (g["transition"]["from"], g["transition"]["to"])
        for g in SM_ENTRY["guards"]
    }
    expected = {
        ("NOT_STARTED", "BLOCKED"),
        ("IN_PRODUCTION", "BLOCKED"),
        ("NOT_STARTED", "CANCELLED"),
        ("IN_PRODUCTION", "CANCELLED"),
        ("BLOCKED", "CANCELLED"),
        ("PRODUCTION_COMPLETE", "IN_PRODUCTION"),
    }
    assert declared == expected


def test_guard_fields_exist_in_schema() -> None:
    known = set(PROPS)
    for guard in SM_ENTRY["guards"]:
        for field in guard["fields"]:
            assert field in known, f"守卫引用了不存在的字段 {field}"


# --------------------------------------------------------------------------- #
# 3. 守卫闸门真的会拦（纯函数注入坏守卫）
# --------------------------------------------------------------------------- #
def test_guard_problems_accepts_current_contract() -> None:
    assert cc.guard_problems(OBJECT, SM_ENTRY, DEF) == []


def test_guard_problems_rejects_unknown_field() -> None:
    broken = {
        "transitions": SM_ENTRY["transitions"],
        "guards": [
            {
                "transition": {"from": "NOT_STARTED", "to": "BLOCKED"},
                "fields": ["block_reason"],  # 少一个 c
                "rule": "拼错字段名",
            }
        ],
    }
    problems = cc.guard_problems(OBJECT, broken, DEF)
    assert any("block_reason" in p for p in problems), problems


def test_guard_problems_rejects_illegal_transition() -> None:
    broken = {
        "transitions": SM_ENTRY["transitions"],
        "guards": [
            {
                "transition": {"from": "CANCELLED", "to": "IN_PRODUCTION"},
                "fields": ["rework_basis_ref"],
                "rule": "给终态造一条出边",
            }
        ],
    }
    problems = cc.guard_problems(OBJECT, broken, DEF)
    assert any("未登记的转移" in p for p in problems), problems


def test_guard_problems_rejects_text_transition() -> None:
    """v1 的「A→B」字符串必须被拒绝：状态值不允许再从文本里解析。"""
    broken = {
        "transitions": SM_ENTRY["transitions"],
        "guards": [
            {
                "transition": "NOT_STARTED→BLOCKED",
                "fields": ["blocked_reason"],
                "rule": "旧格式 fiat lux",
            }
        ],
    }
    problems = cc.guard_problems(OBJECT, broken, DEF)
    assert any("结构化边" in p and "字符串解析" in p for p in problems), problems


def test_guard_problems_rejects_missing_rule() -> None:
    broken = {
        "transitions": SM_ENTRY["transitions"],
        "guards": [
            {
                "transition": {"from": "NOT_STARTED", "to": "BLOCKED"},
                "fields": ["blocked_reason"],
            }
        ],
    }
    problems = cc.guard_problems(OBJECT, broken, DEF)
    assert any("缺字段" in p for p in problems), problems


def test_guard_gate_passes_on_repository_contract() -> None:
    assert cc.check_state_machine_guards() is True


# --------------------------------------------------------------------------- #
# 4. 状态证据：payload 级正反向
# --------------------------------------------------------------------------- #
def test_base_payload_is_valid() -> None:
    assert _violations(DEF, BASE) == []


def test_blocked_requires_reason() -> None:
    assert _violations(DEF, _shot(status="BLOCKED", blocked_reason=None))


def test_blocked_reason_must_not_be_blank() -> None:
    """required 只检查键存在：空串照样合法，「可追溯」就成了空话。"""
    assert _violations(DEF, _shot(status="BLOCKED", blocked_reason=""))


def test_cancelled_evidence_must_not_be_blank() -> None:
    problems = _violations(
        DEF,
        _shot(status="CANCELLED", cancellation_ref="", cancelled_at="2026-11-18T00:00:00Z"),
    )
    assert problems


def test_blocked_reason_must_be_empty_when_not_blocked() -> None:
    problems = _violations(DEF, _shot(status="IN_PRODUCTION", blocked_reason="旧原因"))
    assert problems, "非 BLOCKED 状态还挂着阻塞原因，是没清理的状态残渣"


def test_cancelled_requires_authorization_and_time() -> None:
    no_ref = _violations(
        DEF,
        _shot(status="CANCELLED", cancellation_ref=None, cancelled_at="2026-11-18T00:00:00Z"),
    )
    assert any("cancellation_ref" in p for p in no_ref)

    no_time = _violations(
        DEF, _shot(status="CANCELLED", cancellation_ref="cancel-1", cancelled_at=None)
    )
    assert any("cancelled_at" in p for p in no_time)

    both = _violations(
        DEF,
        _shot(status="CANCELLED", cancellation_ref="cancel-1", cancelled_at="2026-11-18T00:00:00Z"),
    )
    assert both == []


def test_cancellation_evidence_must_be_empty_when_not_cancelled() -> None:
    problems = _violations(DEF, _shot(status="IN_PRODUCTION", cancellation_ref="cancel-1"))
    assert problems


def test_rework_basis_must_be_empty_when_not_in_production() -> None:
    problems = _violations(DEF, _shot(status="NOT_STARTED", rework_basis_ref="rework-1"))
    assert problems, "没在生产却挂着返工依据，说明状态与证据脱节"


def test_in_production_may_carry_rework_basis() -> None:
    assert _violations(DEF, _shot(status="IN_PRODUCTION", rework_basis_ref="rework-1")) == []


def test_version_pointer_is_required() -> None:
    payload = _shot()
    payload.pop("shot_revision_id")
    assert any("shot_revision_id" in p for p in _violations(DEF, payload))


def test_unknown_status_is_rejected() -> None:
    for bad in ("DELIVERED", "IN_REVIEW", "PRODUCTION_COMPLETE_PENDING"):
        assert _violations(DEF, _shot(status=bad)), f"{bad} 不应被接受"


# --------------------------------------------------------------------------- #
# 5. 字段边界（防越权字段回流）
# --------------------------------------------------------------------------- #
def test_content_and_spec_fields_are_not_on_shot() -> None:
    forbidden = {
        "description",
        "narrative_intent",
        "character_refs",
        "location_ref",
        "prop_refs",
        "creative_autonomy",
        "constraint_pack_id",
        "locked_dimensions",
        "prompt",
        "negative_prompt",
        "camera_params",
        "timecode",
        "duration_ms",
    }
    leaked = forbidden & set(PROPS)
    assert not leaked, f"这些字段属于 ShotRevision / ShotSpec / ConstraintPack，不该出现在 Shot: {leaked}"


def test_episode_id_is_not_stored_on_shot() -> None:
    """归属链 scene_id → Scene → Episode → Project，多存一份就是第三套归属事实。"""
    assert "episode_id" not in PROPS
    excluded = {
        e.get("field") for e in DEF["x-afs-exclusions"] if isinstance(e, dict)
    }
    assert "episode_id" in excluded, "排除项必须写明，否则下一个人会以为是漏了"


def test_gate_results_are_not_status_or_fields() -> None:
    """Gate 3/4 结论归 GateInstance 与不可变交付产物，不得成为字段或状态。"""
    for token in ("gate3_pass", "gate4_pass", "delivered"):
        assert not any(token in name.lower() for name in PROPS), token


def test_required_covers_the_whole_envelope() -> None:
    assert set(DEF["required"]) >= ENVELOPE


def test_field_groups_cover_every_property() -> None:
    groups = DEF["x-afs-field-groups"]
    grouped = {field for fields in groups.values() for field in fields}
    assert grouped == set(PROPS), (
        f"未分组字段 {set(PROPS) - grouped} / 分组里多出的字段 {grouped - set(PROPS)}"
    )


def test_scene_id_is_the_only_parent_anchor() -> None:
    """Shot 上只允许 scene_id + project_id 两个归属字段。"""
    parents = {"scene_id", "project_id"} & set(DEF["required"])
    assert parents == {"scene_id", "project_id"}


# --------------------------------------------------------------------------- #
# 6. 缺口登记与契约联动
# --------------------------------------------------------------------------- #
def test_invariants_are_registered() -> None:
    invariants = {i["id"]: i for i in DEF["x-afs-invariants"]}
    assert set(invariants) == {f"SH-{n}" for n in range(1, 9)}, sorted(invariants)
    for inv in invariants.values():
        assert inv["enforced_by"] in cc.INVARIANT_LEVELS
        assert inv["statement"].strip()

    # 这两条能被 schema 表达，标成 RUNTIME_PENDING 就是自降标准
    assert invariants["SH-5"]["enforced_by"] == "SCHEMA"  # 状态证据自洽
    assert invariants["SH-8"]["enforced_by"] == "SCHEMA"  # 进度不越权


def test_runtime_only_invariants_are_declared_pending() -> None:
    """仍未落地的两条必须留在 PENDING，不许被"顺手标成已落地"。

    A13 之前这里有 6 条（SH-1/2/3/4/6/7）。落地 4 条后只剩两条真实缺口：
    - SH-2 的后半句「Shot 与同一 shot_id 的所有修订父级（project/episode/scene）
      必须完全一致」没有复合外键承载——修订表只有 (project_id, shot_id) 一条外键；
    - SH-4「版本指针必须指向存在且 ACTIVE 的修订」在库层没有约束
      （shot_revision_id 不是外键），只由服务写入时保证。
    把这两条也标成 ENFORCED 就是把"服务写对了"当成"不变量被强制了"。
    """
    pending = {
        i["id"] for i in DEF["x-afs-invariants"] if i["enforced_by"] == "RUNTIME_PENDING"
    }
    assert pending == {"SH-2", "SH-4"}, f"缺口集合变了，请逐条复核而不是改断言：{pending}"


def test_confirm_action_covers_both_objects() -> None:
    """首次确认原子创建 Shot + 首个 ShotRevision，资源类型必须两者都声明。"""
    action = next(
        a
        for a in ACTIONS["actions"]
        if a["action_key"] == "SHOT.REVISION.CONFIRM"
    )
    covered = {part.strip() for part in action["resource_type"].split("/")}
    assert covered == {"Shot", "ShotRevision"}
    assert "原子创建" in action["approval_requirement"]


def test_both_objects_have_schema_and_no_open_gap() -> None:
    assert {"Shot", "ShotRevision"} <= set(SCHEMA["definitions"])
    assert "Shot" not in GAPS["gaps"]
    assert "ShotRevision" not in GAPS["gaps"]


def test_coverage_ledger_counts_match_definitions() -> None:
    coverage = SCHEMA["x-afs-p0-coverage"]
    assert coverage["total"] == len(SCHEMA["definitions"])
    assert len(coverage["modeled"]) == coverage["total"]
    assert OBJECT in coverage["modeled"]


def test_registry_counts_are_consistent() -> None:
    assert STATE_MACHINES["total_objects"] == len(STATE_MACHINES["objects"])
    assert ENUMS["object_count"] == len(ENUMS["objects"])

# --------------------------------------------------------------------------- #
# 7. 跨对象一致性：契约的可执行示例（不是运行时实现）
# --------------------------------------------------------------------------- #
def _collection_violations(shots: list[dict], revisions: list[dict]) -> list[str]:
    """把 SH-2 / SH-3 / SR-5 写成可执行判定，供实现方对照。

    明确边界：这是**契约的可执行示例**，只在测试里跑。运行时该落的位置是服务层
    与数据库约束——``(project_id, shot_code)`` 唯一索引、修订表的
    ``(shot_id, project_id, episode_id, scene_id)`` 复合外键。运行时落地后本函数
    应当删除，改用真实服务层校验；留着它只会让「测试通过」与「系统正确」之间
    多一层假象。
    """
    out: list[str] = []
    seen: dict[tuple[str, str], str] = {}
    for shot in shots:
        key = (shot["project_id"], shot["shot_code"])
        if key in seen and seen[key] != shot["shot_id"]:
            out.append(f"项目 {key[0]} 内 shot_code 重复: {key[1]}")
        seen[key] = shot["shot_id"]

    index = {shot["shot_id"]: shot for shot in shots}
    by_shot: dict[str, list[dict]] = {}
    for revision in revisions:
        by_shot.setdefault(revision["shot_id"], []).append(revision)

    for shot_id, revs in by_shot.items():
        parents = {
            (r["project_id"], r["episode_id"], r["scene_id"]) for r in revs
        }
        if len(parents) > 1:
            out.append(f"{shot_id} 的修订父级不一致: {sorted(parents)}")
        shot = index.get(shot_id)
        if shot is None:
            out.append(f"{shot_id} 的修订指向不存在的 Shot")
            continue
        for project_id, _episode_id, scene_id in parents:
            if project_id != shot["project_id"]:
                out.append(f"{shot_id} 的修订挂在项目 {project_id}，Shot 在 {shot['project_id']}")
            if scene_id != shot["scene_id"]:
                out.append(
                    f"{shot_id} 的修订挂在场次 {scene_id}，Shot 在 {shot['scene_id']}：跨场次改挂"
                )
    return out


def _revision(shot_id: str, **overrides) -> dict:
    payload = {
        "shot_id": shot_id,
        "project_id": "prj_01J",
        "episode_id": "ep_01J",
        "scene_id": "sc_01J",
    }
    payload.update(overrides)
    return payload


def test_collection_is_consistent_for_a_clean_case() -> None:
    shots = [BASE, _shot(shot_id="sh_02J", shot_code="EP001-SC003-SH013")]
    revisions = [
        _revision("sh_01J"),
        _revision("sh_01J", scene_id="sc_01J"),
        _revision("sh_02J"),
    ]
    assert _collection_violations(shots, revisions) == []


def test_duplicate_shot_code_in_same_project_is_rejected() -> None:
    shots = [BASE, _shot(shot_id="sh_02J", shot_code="EP001-SC003-SH012")]
    problems = _collection_violations(shots, [])
    assert any("shot_code 重复" in p for p in problems), problems


def test_same_shot_code_across_projects_is_allowed() -> None:
    """8.2 只要求业务短编号唯一性落在项目内，跨项目撞号不是错误。"""
    shots = [BASE, _shot(shot_id="sh_02J", project_id="prj_02J")]
    assert _collection_violations(shots, []) == []


def test_revision_cannot_be_reattached_to_another_scene() -> None:
    """SR-5 / SH-2：同一 shot_id 的修订不得跨场次改挂。"""
    problems = _collection_violations([BASE], [_revision("sh_01J", scene_id="sc_02J")])
    assert any("跨场次改挂" in p for p in problems), problems


def test_revision_parents_must_not_drift_among_themselves() -> None:
    problems = _collection_violations(
        [BASE], [_revision("sh_01J"), _revision("sh_01J", scene_id="sc_02J")]
    )
    assert any("父级不一致" in p for p in problems), problems


def test_revision_must_point_to_an_existing_shot() -> None:
    problems = _collection_violations([BASE], [_revision("sh_ghost")])
    assert any("不存在" in p for p in problems), problems
