"""状态枚举一致性闸门的回归测试（七个负向场景 + 派生器幂等）。

这一轮把「三份手写状态列表」收敛成「一处权威 + 两处派生」，靠的不是纪律，而是闸门。
本文件的作用是证明闸门**真的会拦**：每个用例都注入一处具体的漂移，断言闸门报出
问题，而不是只跑一遍现有契约拿到 PASS。

七个必须报红的场景（对应契约治理任务的验收条款）：

1. 从 Schema 删掉一个合法状态；
2. 给 Schema 多加一个不存在的状态；
3. 只改派生文件（enum_registry），不动权威源；
4. 状态机新增状态但 Schema 未同步；
5. 转移边引用未登记的状态；
6. 把展示别名 / STALE 标记 / 检查项编号当权威状态值；
7. 删掉缺口登记，但对象仍未建模。

再加三类「台账不能被绕过」的用例：分类漏登记、分类自证缺失、lifecycle 与状态机
不对应。最后是派生器幂等（重复生成结果不变）与运行时取值覆盖（旁支状态确实进了
Domain Enum 与数据库约束）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import contract_check as cc  # noqa: E402
import gen_status_contracts as gs  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = ROOT / "contracts" / "schemas" / "object_schemas.schema.json"
STATE_MACHINES_PATH = ROOT / "contracts" / "state_machines" / "state_machines.yaml"
ENUM_REGISTRY_PATH = ROOT / "contracts" / "enums" / "enum_registry.yaml"
TAXONOMY_PATH = ROOT / "contracts" / "enums" / "status_field_taxonomy.yaml"
LAYERS_PATH = ROOT / "contracts" / "enums" / "status_layers.yaml"


# --------------------------------------------------------------------------- #
# 夹具：从真实契约读一份快照（用例只改副本，不动仓库文件）
# --------------------------------------------------------------------------- #
def _definitions() -> dict[str, Any]:
    data = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    return data["definitions"]


def _sm_objects() -> list[dict[str, Any]]:
    data = yaml.safe_load(STATE_MACHINES_PATH.read_text(encoding="utf-8"))
    return data["objects"]


def _taxonomy() -> dict[str, Any]:
    return yaml.safe_load(TAXONOMY_PATH.read_text(encoding="utf-8"))


def _layers() -> dict[str, Any]:
    return yaml.safe_load(LAYERS_PATH.read_text(encoding="utf-8"))


def _classes() -> dict[str, str]:
    return {
        str(entry["object"]): str(entry["class"])
        for entry in _taxonomy()["objects"]
    }


def _scene_def() -> dict[str, Any]:
    return json.loads(json.dumps(_definitions()["Scene"]))


# --------------------------------------------------------------------------- #
# 0. 正向：当前契约必须全绿（否则下面所有负向断言都失去意义）
# --------------------------------------------------------------------------- #
def test_current_contract_is_consistent() -> None:
    assert cc.check_status_enum_consistency() is True


def test_derived_files_match_authority() -> None:
    """派生文件与权威源必须逐字节一致（漂移即为 FAIL）。"""
    assert gs.drift_problems() == []


def test_enum_registry_is_sorted_projection() -> None:
    """注册表是派生结果：对象按字母序、状态按字母序，手改必然破坏这两条。"""
    states = gs.lifecycle_states()
    registry = yaml.safe_load(ENUM_REGISTRY_PATH.read_text(encoding="utf-8"))
    names = [entry["object"] for entry in registry["objects"]]
    assert names == sorted(names)
    for entry in registry["objects"]:
        assert entry["states"] == sorted(states[entry["object"]])


# --------------------------------------------------------------------------- #
# 1. 从 Schema 删掉一个合法状态 → 报红
# --------------------------------------------------------------------------- #
def test_missing_state_in_schema_is_reported() -> None:
    definitions = _definitions()
    definition = json.loads(json.dumps(definitions["Scene"]))
    definition["properties"]["status"]["enum"].remove("ARCHIVED")
    sm_states = cc.state_machine_states(_sm_objects())

    problems = cc.schema_enum_problems(
        {"Scene": definition}, sm_states, {"Scene": cc.CLASS_LIFECYCLE}
    )
    assert any("缺权威集合里的值" in p and "ARCHIVED" in p for p in problems), problems


# --------------------------------------------------------------------------- #
# 2. 给 Schema 多加一个不存在的状态 → 报红
# --------------------------------------------------------------------------- #
def test_extra_state_in_schema_is_reported() -> None:
    definition = json.loads(json.dumps(_definitions()["Scene"]))
    definition["properties"]["status"]["enum"].append("IN_REVIEW")
    sm_states = cc.state_machine_states(_sm_objects())

    problems = cc.schema_enum_problems(
        {"Scene": definition}, sm_states, {"Scene": cc.CLASS_LIFECYCLE}
    )
    assert any("权威集合里没有的值" in p and "IN_REVIEW" in p for p in problems), problems


def test_bare_string_status_is_reported() -> None:
    """lifecycle 对象的 status 退回裸 string，等于放开任意字符串（C.3.1 违规）。"""
    definition = json.loads(json.dumps(_definitions()["Scene"]))
    definition["properties"]["status"] = {"type": "string"}
    sm_states = cc.state_machine_states(_sm_objects())

    problems = cc.schema_enum_problems(
        {"Scene": definition}, sm_states, {"Scene": cc.CLASS_LIFECYCLE}
    )
    assert any("无约束裸字符串" in p for p in problems), problems


# --------------------------------------------------------------------------- #
# 3. 只改派生文件（enum_registry）→ 报红
# --------------------------------------------------------------------------- #
def test_registry_only_edit_is_reported() -> None:
    registry = yaml.safe_load(ENUM_REGISTRY_PATH.read_text(encoding="utf-8"))
    for entry in registry["objects"]:
        if entry["object"] == "Scene":
            entry["states"] = ["DRAFT", "ACTIVE"]  # 悄悄删掉 ARCHIVED
    sm_states = cc.state_machine_states(_sm_objects())

    problems = cc.enum_registry_problems(registry["objects"], sm_states)
    assert any("Scene" in p and "ARCHIVED" in p for p in problems), problems


def test_registry_only_edit_is_caught_by_generator(tmp_path: Path, monkeypatch) -> None:
    """派生器的 --check 就是「重算一遍比字节」，手改派生文件必然被发现。

    这里只加一行注释，不改任何状态值：语义比对全绿，但字节漂移必须报红。
    这样才能证明「闸门接住了派生器」，而不是靠值比对顺带蒙对。
    """
    tampered = ENUM_REGISTRY_PATH.read_text(encoding="utf-8").replace(
        "# 枚举注册表（派生文件，禁止手改）",
        "# 手改的注释：语义没变，但派生文件不再等于重算结果\n# 枚举注册表（派生文件，禁止手改）",
        1,
    )
    path = tmp_path / "enum_registry.yaml"
    path.write_text(tampered, encoding="utf-8")
    monkeypatch.setattr(gs, "ENUM_REGISTRY", path)
    monkeypatch.setattr(cc, "ENUM_REGISTRY_PATH", path)

    problems = gs.drift_problems()
    assert any("enum_registry.yaml" in p for p in problems), problems

    # 闸门本身也必须拦住它：CI 只跑 contract_check，不会单独跑派生器 --check。
    assert cc.check_status_enum_consistency() is False


# --------------------------------------------------------------------------- #
# 4. 状态机新增状态但 Schema 未同步 → 报红
# --------------------------------------------------------------------------- #
def test_state_machine_added_state_without_schema_sync_is_reported() -> None:
    sm_objects = _sm_objects()
    for entry in sm_objects:
        if entry["object"] == "Scene":
            entry["allowed_states"] = sorted([*entry["allowed_states"], "SUSPENDED"])
    sm_states = cc.state_machine_states(sm_objects)

    problems = cc.schema_enum_problems(
        _definitions(), sm_states, _classes()
    )
    assert any("Scene.status.enum 缺权威集合里的值" in p for p in problems), problems


def test_state_machine_without_allowed_states_is_rejected() -> None:
    """状态机条目缺 allowed_states 视为契约损坏（不能静默当空集）。"""
    with pytest.raises(ValueError, match="allowed_states"):
        cc.state_machine_states([{"object": "Scene", "transitions": []}])


# --------------------------------------------------------------------------- #
# 5. 转移边引用未登记状态 → 报红
# --------------------------------------------------------------------------- #
def test_transition_to_unknown_state_is_reported() -> None:
    sm_objects = [
        {
            "object": "Scene",
            "allowed_states": ["DRAFT", "ACTIVE"],
            "transitions": [{"from": "DRAFT", "to": "ARCHIVED"}],
        }
    ]
    problems = cc.transition_target_problems(sm_objects)
    assert any("ARCHIVED" in p and "未登记状态" in p for p in problems), problems


def test_transition_from_unknown_state_is_reported() -> None:
    sm_objects = [
        {
            "object": "Scene",
            "allowed_states": ["ACTIVE"],
            "transitions": [{"from": "DRAFT", "to": "ACTIVE"}],
        }
    ]
    assert any("DRAFT" in p for p in cc.transition_target_problems(sm_objects))


def test_side_branch_states_do_not_need_edges() -> None:
    """允许状态可以没有入边（旁支），这条不能误报——它是本轮设计的核心。"""
    sm_objects = [
        {
            "object": "AssetVersion",
            "allowed_states": ["GENERATED", "QUARANTINED"],
            "transitions": [],
        }
    ]
    assert cc.transition_target_problems(sm_objects) == []


def test_authoritative_post_publish_edges_are_registered() -> None:
    """正文明确授权的发布后/取消/取代边必须在转移表里（PR #14 评审补齐）。

    这些边在 v2 结构化迁移时漏抄，导致「契约声明的状态」与「可执行的状态转移」
    脱节。逐条锁定，防止未来误删后闸门仍然全绿。
    """
    edges = {
        obj["object"]: {(t["from"], t["to"]) for t in obj.get("transitions", [])}
        for obj in _sm_objects()
    }
    required = {
        # 31.5「发布后可 UNLISTED / TAKEN_DOWN / ARCHIVED」
        "CommunityPost": {
            ("PUBLISHED", "UNLISTED"),
            ("PUBLISHED", "TAKEN_DOWN"),
            ("PUBLISHED", "ARCHIVED"),
        },
        # 34.8「发布后可 UNLISTED / TAKEN_DOWN / REVOKED / ARCHIVED」
        "CreatorRelease": {
            ("PUBLISHED", "UNLISTED"),
            ("PUBLISHED", "TAKEN_DOWN"),
            ("PUBLISHED", "REVOKED"),
            ("PUBLISHED", "ARCHIVED"),
        },
        # 33.3「旧版本 → SUPERSEDED/STALE/ARCHIVED」，DRAFT 侧两条本轮补齐
        "TimelineDraft": {("DRAFT", "STALE"), ("DRAFT", "ARCHIVED")},
        # 8.6「READY/QUEUED 允许取消」
        "Task": {("READY", "CANCEL_REQUESTED"), ("QUEUED", "CANCEL_REQUESTED")},
    }
    for obj_name, expected in required.items():
        missing = expected - edges.get(obj_name, set())
        assert not missing, f"{obj_name} 缺正文授权边: {sorted(missing)}"


# --------------------------------------------------------------------------- #
# 6. 展示别名 / STALE 标记 / 检查项编号混入权威枚举 → 报红
# --------------------------------------------------------------------------- #
def test_display_alias_in_allowed_states_is_reported() -> None:
    layers = _layers()
    sm_states = cc.state_machine_states(_sm_objects())
    sm_states["ShowcaseSnapshot"] = sm_states["ShowcaseSnapshot"] | {"SHOWCASE_OUTDATED"}

    problems = cc.alias_layer_problems(layers, sm_states)
    assert any("SHOWCASE_OUTDATED" in p for p in problems), problems


def test_cross_domain_mark_in_allowed_states_is_reported() -> None:
    layers = _layers()
    sm_states = cc.state_machine_states(_sm_objects())
    sm_states["ReverseAnalysisReport"] = sm_states["ReverseAnalysisReport"] | {"REVERSE_STALE"}

    problems = cc.alias_layer_problems(layers, sm_states)
    assert any("REVERSE_STALE" in p for p in problems), problems


def test_prohibited_gate_number_is_reported() -> None:
    """PG0 / PG3 是 Publication Gate 的检查项编号，曾被当成状态值登记。"""
    layers = _layers()
    sm_states = cc.state_machine_states(_sm_objects())
    sm_states["CommunityPost"] = sm_states["CommunityPost"] | {"PG3"}

    problems = cc.alias_layer_problems(layers, sm_states)
    assert any("PG3" in p for p in problems), problems


def test_prohibited_value_respects_object_scope() -> None:
    """PASS 在 CreatorGate 是合法状态，禁令必须按对象收窄，不能一刀切。"""
    layers = _layers()
    sm_states = cc.state_machine_states(_sm_objects())
    assert "PASS" in sm_states["CreatorGate"]
    assert cc.alias_layer_problems(layers, sm_states) == []


def test_alias_without_landing_point_is_reported() -> None:
    layers = {
        "state_projection_aliases": [
            {"alias": "GHOST_ALIAS", "object": "ShowcaseSnapshot", "maps_to_state": "NOT_A_STATE"}
        ],
        "cross_domain_marks": [],
        "prohibited_state_values": [],
    }
    problems = cc.alias_layer_problems(layers, cc.state_machine_states(_sm_objects()))
    assert any("NOT_A_STATE" in p for p in problems), problems


# --------------------------------------------------------------------------- #
# 7. 删掉缺口登记，但对象仍未建模 → 报红
# --------------------------------------------------------------------------- #
def test_missing_schema_without_gap_entry_is_reported() -> None:
    sm_states = cc.state_machine_states(_sm_objects())
    problems = cc.schema_gap_problems([], sm_states, _definitions())
    assert any("PublicationReview" in p for p in problems), problems


def test_stale_gap_entry_is_reported() -> None:
    """对象补了 schema 却留着缺口条目，同样是账目失真（陈旧债务）。"""
    sm_states = cc.state_machine_states(_sm_objects())
    gaps = [
        {
            "object": "Scene",
            "reason": "假设无 schema",
            "contract_ref": "x",
            "unblocks_when": "y",
        }
    ]
    problems = cc.schema_gap_problems(gaps, sm_states, _definitions())
    assert any("已有 object schema" in p for p in problems), problems


def test_gap_entry_without_debt_fields_is_reported() -> None:
    sm_states = cc.state_machine_states(_sm_objects())
    gaps = [{"object": "PublicationReview", "reason": "只说缺"}]
    problems = cc.schema_gap_problems(gaps, sm_states, _definitions())
    assert any("缺字段" in p for p in problems), problems


# --------------------------------------------------------------------------- #
# 台账不能被绕过：漏登记 / 幽灵条目 / 结论无论证 / 分类与状态机打架
# --------------------------------------------------------------------------- #
def test_unregistered_status_field_is_reported() -> None:
    """新对象带着裸 status 进来时，台账缺条目必须报红。"""
    definitions = _definitions()
    definitions["GhostObject"] = {
        "properties": {"status": {"type": "string"}},
    }
    problems = cc.taxonomy_problems(
        _taxonomy()["objects"], cc.state_machine_states(_sm_objects()), definitions
    )
    assert any("GhostObject" in p for p in problems), problems


def test_ghost_taxonomy_entry_is_reported() -> None:
    taxonomy = [*_taxonomy()["objects"], {"object": "GhostObject", "class": "lifecycle"}]
    problems = cc.taxonomy_problems(
        taxonomy, cc.state_machine_states(_sm_objects()), _definitions()
    )
    assert any("GhostObject" in p for p in problems), problems


def test_envelope_only_without_reason_is_reported() -> None:
    taxonomy = [
        entry
        for entry in _taxonomy()["objects"]
        if entry["object"] != "CameraRig"
    ]
    taxonomy.append({"object": "CameraRig", "class": "envelope_only", "basis": "immutable_record"})
    problems = cc.taxonomy_problems(
        taxonomy, cc.state_machine_states(_sm_objects()), _definitions()
    )
    assert any("CameraRig" in p and "reason" in p for p in problems), problems


def test_lifecycle_gap_without_debt_is_reported() -> None:
    taxonomy = [
        entry for entry in _taxonomy()["objects"] if entry["object"] != "CameraRig"
    ]
    taxonomy.append({"object": "CameraRig", "class": "lifecycle_gap"})
    problems = cc.taxonomy_problems(
        taxonomy, cc.state_machine_states(_sm_objects()), _definitions()
    )
    assert any("CameraRig" in p and "债务字段" in p for p in problems), problems


def test_lifecycle_class_without_state_machine_is_reported() -> None:
    taxonomy = [
        entry for entry in _taxonomy()["objects"] if entry["object"] != "CameraRig"
    ]
    taxonomy.append({"object": "CameraRig", "class": "lifecycle"})
    problems = cc.taxonomy_problems(
        taxonomy, cc.state_machine_states(_sm_objects()), _definitions()
    )
    assert any("CameraRig" in p and "没有它的状态机" in p for p in problems), problems


def test_envelope_only_cannot_coexist_with_state_machine() -> None:
    taxonomy = [
        entry
        for entry in _taxonomy()["objects"]
        if entry["object"] != "CameraRig"
    ]
    taxonomy.append(
        {
            "object": "CameraRig",
            "class": "envelope_only",
            "basis": "immutable_record",
            "reason": "假设它是记录",
        }
    )
    sm_states = cc.state_machine_states(_sm_objects())
    sm_states["CameraRig"] = {"DRAFT"}
    problems = cc.taxonomy_problems(taxonomy, sm_states, _definitions())
    assert any("两套并存" in p for p in problems), problems


# --------------------------------------------------------------------------- #
# 状态机文件自身的账目
# --------------------------------------------------------------------------- #
def test_total_objects_mismatch_is_reported() -> None:
    sm_data = {
        "objects": [{"object": "Scene", "allowed_states": ["DRAFT"]}],
        "total_objects": 2,
        "conflict_count": 0,
    }
    problems = cc.governance_problems(sm_data, _definitions())
    assert any("total_objects" in p for p in problems), problems


def test_conflict_count_mismatch_is_reported() -> None:
    sm_data = {
        "objects": [
            {"object": "Scene", "allowed_states": ["DRAFT"], "conflict": True}
        ],
        "total_objects": 1,
        "conflict_count": 0,
    }
    problems = cc.governance_problems(sm_data, _definitions())
    assert any("conflict_count" in p for p in problems), problems


def test_projection_with_schema_is_reported() -> None:
    """UI 投影不是持久化对象：它若进了 object_schemas，就会有人给它建表。"""
    sm_data = {
        "objects": [],
        "total_objects": 0,
        "conflict_count": 0,
        "projections": [{"object": "Scene", "allowed_states": ["DRAFT"]}],
    }
    problems = cc.governance_problems(sm_data, _definitions())
    assert any("UI 投影" in p for p in problems), problems


# --------------------------------------------------------------------------- #
# 运行时：旁支状态必须真的进 Domain Enum 与数据库约束
# --------------------------------------------------------------------------- #
def test_asset_enum_covers_all_allowed_states() -> None:
    """AssetStatus 必须收全部十态：只收有入边的五态会让合法旁支值反序列化失败。"""
    from services.asset_service.domain.enums import AssetStatus

    sm_states = cc.state_machine_states(_sm_objects())
    assert {s.value for s in AssetStatus} == sm_states["AssetVersion"]


def test_status_check_sql_is_contract_shaped() -> None:
    from packages.shared.status_constraint import status_check_sql

    assert (
        status_check_sql({"DRAFT", "ACTIVE"}) == "status IN ('ACTIVE', 'DRAFT')"
    )
    with pytest.raises(ValueError):
        status_check_sql([])


def test_implemented_tables_declare_status_check() -> None:
    """四张已落库表都必须有 status CHECK（少一张就是「库里还收任意字符串」）。"""
    import services.asset_service.repository.orm
    import services.project_planning.repository.orm  # noqa: F401
    from packages.shared.database import Base

    sm_states = cc.state_machine_states(_sm_objects())
    for table_name, obj_name in cc._STATUS_TABLES.items():
        table = Base.metadata.tables[table_name]
        names = {getattr(c, "name", "") for c in table.constraints}
        assert f"ck_{table.name}_status" in names, table_name
        assert sm_states[obj_name], obj_name


# --------------------------------------------------------------------------- #
# ADR-R21：信封哨兵 + 双 status 字段权威裁定
# --------------------------------------------------------------------------- #
def _envelope_status(name: str) -> dict[str, Any]:
    definition = json.loads(json.dumps(_definitions()[name]))
    return definition["properties"]["status"]


def test_envelope_only_status_uses_sentinel_not_bare_string() -> None:
    """承认没有生命周期，就不允许继续收任意字符串。"""
    sentinel = cc.envelope_only_sentinel(_layers())
    assert _envelope_status("QCIssue")["enum"] == [sentinel]
    assert _envelope_status("ModelInvocation")["enum"] == [sentinel]
    assert _envelope_status("ExternalToolAdapterManifest")["enum"] == [sentinel]


def test_envelope_only_bare_string_is_reported() -> None:
    definition = json.loads(json.dumps(_definitions()["QCIssue"]))
    definition["properties"]["status"] = {"type": "string"}
    classes = {**_classes(), "QCIssue": cc.CLASS_ENVELOPE_ONLY}
    problems = cc.schema_enum_problems(
        {"QCIssue": definition}, cc.state_machine_states(_sm_objects()), classes, _layers()
    )
    assert any("无约束裸字符串" in p for p in problems), problems


def test_envelope_only_extra_lifecycle_value_is_reported() -> None:
    definition = json.loads(json.dumps(_definitions()["QCIssue"]))
    definition["properties"]["status"]["enum"] = ["NO_LIFECYCLE", "OPEN"]
    classes = {**_classes(), "QCIssue": cc.CLASS_ENVELOPE_ONLY}
    problems = cc.schema_enum_problems(
        {"QCIssue": definition}, cc.state_machine_states(_sm_objects()), classes, _layers()
    )
    assert any("哨兵" in p and "OPEN" in p for p in problems), problems


def test_envelope_only_without_unblocks_when_is_reported() -> None:
    taxonomy = []
    for entry in _taxonomy()["objects"]:
        if entry["object"] == "QCIssue":
            entry = {k: v for k, v in entry.items() if k != "unblocks_when"}
        taxonomy.append(entry)
    problems = cc.taxonomy_problems(
        taxonomy, cc.state_machine_states(_sm_objects()), _definitions()
    )
    assert any("QCIssue" in p and "unblocks_when" in p for p in problems), problems


def test_envelope_only_basis_must_be_in_closed_enum() -> None:
    """basis 拼错或自造类别必须报红——台账自称 basis_kinds 是封闭枚举（PR #14 评审）。

    只校验「非空」会让 ``immutabel_record`` 这类拼写错误静默通过，封闭枚举就成了
    纯文案。这里显式传入合法集合注入一个非法值，验证闸门真的拒绝。
    """
    taxonomy = []
    for entry in _taxonomy()["objects"]:
        if entry["object"] == "QCIssue":
            entry = {**entry, "basis": "immutabel_record"}  # 故意拼错
        taxonomy.append(entry)
    problems = cc.taxonomy_problems(
        taxonomy,
        cc.state_machine_states(_sm_objects()),
        _definitions(),
        basis_kinds={"immutable_record", "child_structure", "derived_projection", "status_split"},
    )
    assert any("QCIssue" in p and "basis_kinds" in p for p in problems), problems


def test_envelope_only_basis_accepts_declared_kind() -> None:
    """合法 basis 不应触发封闭枚举告警（防止闸门过严把正常台账误判为红）。"""
    problems = cc.taxonomy_problems(
        _taxonomy()["objects"],
        cc.state_machine_states(_sm_objects()),
        _definitions(),
        basis_kinds={"immutable_record", "child_structure", "derived_projection", "status_split"},
    )
    assert not any("basis_kinds" in p for p in problems), problems


def test_field_authority_is_consistent_with_schema() -> None:
    path = ROOT / "contracts" / "enums" / "field_authority.yaml"
    assert cc.field_authority_problems(path, _definitions()) == []


def test_field_authority_rejects_unknown_field(tmp_path: Path) -> None:
    data = yaml.safe_load(
        (ROOT / "contracts" / "enums" / "field_authority.yaml").read_text(encoding="utf-8")
    )
    data["objects"][0]["authoritative_fields"] = ["ghost_status"]
    # 不碰仓库文件：写进 pytest 的临时目录再校验
    tmp = tmp_path / "field_authority.yaml"
    tmp.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    problems = cc.field_authority_problems(tmp, _definitions())
    assert any("ghost_status" in p for p in problems), problems


def test_previz_version_status_is_the_authoritative_lifecycle() -> None:
    entry = next(o for o in _sm_objects() if o["object"] == "PrevizVersion")
    assert set(entry["allowed_states"]) == set(_definitions()["PrevizVersion"]["properties"]["status"]["enum"])
    assert "purity_check_status" not in entry["allowed_states"]
    assert entry["transitions"] == [{"from": "APPROVED", "to": "LOCKED"}]


def test_previz_version_transition_gap_must_be_explained() -> None:
    entry = next(o for o in _sm_objects() if o["object"] == "PrevizVersion")
    assert entry["transitions_gap"]["reason"]
    assert entry["transitions_gap"]["unblocks_when"]
