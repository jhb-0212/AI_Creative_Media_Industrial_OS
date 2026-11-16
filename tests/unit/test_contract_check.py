"""契约一致性校验脚本测试。

覆盖：
1. parse_state_chain 解析器：连续箭头、斜杠多源、中英文分隔符、空串。
2. check_project_state_machine 正向：当前契约与实现一致，应通过。
3. 负向：临时制造不一致（契约多/少转移），应被检出。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

# 将 scripts 加入导入路径（scripts 不是包，直接按模块加载）。
SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import contract_check as cc  # noqa: E402


# --------------------------------------------------------------------------- #
# parse_state_chain 解析器
# --------------------------------------------------------------------------- #
def test_parse_simple_chain() -> None:
    assert cc.parse_state_chain("DRAFT→ACTIVE") == {("DRAFT", "ACTIVE")}


def test_parse_consecutive_arrows() -> None:
    assert cc.parse_state_chain("DRAFT→ACTIVE→ON_HOLD") == {
        ("DRAFT", "ACTIVE"),
        ("ACTIVE", "ON_HOLD"),
    }


def test_parse_slash_multi_source() -> None:
    assert cc.parse_state_chain("DRAFT/ON_HOLD/COMPLETED→ARCHIVED") == {
        ("DRAFT", "ARCHIVED"),
        ("ON_HOLD", "ARCHIVED"),
        ("COMPLETED", "ARCHIVED"),
    }


def test_parse_full_contract_chain() -> None:
    text = (
        "DRAFT→ACTIVE→ON_HOLD；ACTIVE→COMPLETED；"
        "ON_HOLD→ACTIVE；DRAFT/ON_HOLD/COMPLETED→ARCHIVED"
    )
    expected = {
        ("DRAFT", "ACTIVE"),
        ("ACTIVE", "ON_HOLD"),
        ("ACTIVE", "COMPLETED"),
        ("ON_HOLD", "ACTIVE"),
        ("DRAFT", "ARCHIVED"),
        ("ON_HOLD", "ARCHIVED"),
        ("COMPLETED", "ARCHIVED"),
    }
    assert cc.parse_state_chain(text) == expected


def test_parse_ascii_arrows_and_semicolons() -> None:
    assert cc.parse_state_chain("A->B; B/C->D") == {
        ("A", "B"),
        ("B", "D"),
        ("C", "D"),
    }


def test_parse_empty_returns_empty() -> None:
    assert cc.parse_state_chain("") == set()
    assert cc.parse_state_chain("   ") == set()


# --------------------------------------------------------------------------- #
# check_project_state_machine 正向 / 负向
# --------------------------------------------------------------------------- #
def test_check_project_state_machine_passes_on_consistent() -> None:
    """当前契约与实现一致，应返回 True。"""
    assert cc.check_project_state_machine() is True


def test_check_project_state_machine_detects_missing_in_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """契约缺少实现已有转移时，应返回 False（负向）。"""
    # 契约缺少 (ACTIVE, ON_HOLD) 这条实现已有的转移
    fake_transitions = {
        ("DRAFT", "ACTIVE"),
        ("ACTIVE", "COMPLETED"),
        ("ON_HOLD", "ACTIVE"),
        ("DRAFT", "ARCHIVED"),
        ("ON_HOLD", "ARCHIVED"),
        ("COMPLETED", "ARCHIVED"),
    }
    monkeypatch.setattr(cc, "parse_state_chain", lambda text: fake_transitions)
    assert cc.check_project_state_machine() is False


def test_check_project_state_machine_detects_extra_in_impl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """契约存在实现未登记的转移时，应返回 False（负向）。"""
    # 契约多一条实现未登记的转移 (COMPLETED, ACTIVE)
    fake_transitions = {
        ("DRAFT", "ACTIVE"),
        ("ACTIVE", "ON_HOLD"),
        ("ACTIVE", "COMPLETED"),
        ("ON_HOLD", "ACTIVE"),
        ("DRAFT", "ARCHIVED"),
        ("ON_HOLD", "ARCHIVED"),
        ("COMPLETED", "ARCHIVED"),
        ("COMPLETED", "ACTIVE"),  # 实现未登记，应被检出
    }
    monkeypatch.setattr(cc, "parse_state_chain", lambda text: fake_transitions)
    assert cc.check_project_state_machine() is False


# --------------------------------------------------------------------------- #
# 扩展校验：Action/OpenAPI/Permission/Schema
# --------------------------------------------------------------------------- #
def test_normalize_action_key() -> None:
    assert cc._normalize_action_key("PROJECT.CREATE") == "project.create"
    assert cc._normalize_action_key("  Shot.Draft.Create ") == "shot.draft.create"


def test_check_action_openapi_consistency_passes() -> None:
    assert cc.check_action_openapi_consistency() is True


def test_check_permission_matrix_passes() -> None:
    assert cc.check_permission_matrix() is True


def test_check_schema_objects_passes() -> None:
    assert cc.check_schema_objects() is True


def test_action_openapi_mismatch_semantics(monkeypatch, tmp_path) -> None:
    """真实调用被测函数：删掉一个 action 后必须识别出不一致（返回 False）。

    不复制校验逻辑，只构造契约副本，保证用例能随被测实现一起变红。
    """
    import shutil

    import yaml

    contracts = tmp_path / "contracts"
    shutil.copytree(cc.CONTRACTS, contracts)

    registry_path = contracts / "action_registry" / "action_registry.yaml"
    data = yaml.safe_load(registry_path.read_text(encoding="utf-8"))
    data["actions"] = [
        action
        for action in data["actions"]
        if action.get("action_key") != "SCRIPT.IMPORT"
    ]
    data["total_actions"] = len(data["actions"])
    registry_path.write_text(
        yaml.safe_dump(data, allow_unicode=True), encoding="utf-8"
    )

    monkeypatch.setattr(cc, "CONTRACTS", contracts)

    assert cc.check_action_openapi_consistency() is False


# --------------------------------------------------------------------------- #
# 运行时 ↔ 契约覆盖（台账不能撒谎）
# --------------------------------------------------------------------------- #
def test_runtime_contract_coverage_passes() -> None:
    """当前台账、契约与真实路由三方一致。"""
    assert cc.check_runtime_contract_coverage() is True


def test_runtime_coverage_detects_phantom_implementation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """台账声称已实现、运行时却没有该端点 → 必须变红（声明不能撒谎）。"""
    real = cc._runtime_endpoints()
    monkeypatch.setattr(
        cc,
        "_runtime_endpoints",
        lambda: real - {"POST /scripts/import"},
    )
    assert cc.check_runtime_contract_coverage() is False


def test_runtime_coverage_detects_unregistered_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """运行时多出未登记端点 → 必须变红（新端点不能绕过契约）。"""
    real = cc._runtime_endpoints()
    monkeypatch.setattr(
        cc,
        "_runtime_endpoints",
        lambda: real | {"POST /shadow/undocumented"},
    )
    assert cc.check_runtime_contract_coverage() is False


def test_runtime_coverage_detects_unlogged_active_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ACTIVE action 既没实现也没登记原因 → 必须变红。"""
    original = cc._active_action_keys
    monkeypatch.setattr(
        cc, "_active_action_keys", lambda: original() | {"SHOT.GHOST.ACTION"}
    )
    assert cc.check_runtime_contract_coverage() is False


# --------------------------------------------------------------------------- #
# 已实现 action 的对象 schema 覆盖闸门
# --------------------------------------------------------------------------- #
def test_missing_resource_schemas_reports_undefined_and_unexempted() -> None:
    """资源类型未定义且未豁免 → 报 (action, type)；组合类型按 / 拆分。"""
    missing = cc.missing_resource_schemas(
        {"A.CREATE", "B.CREATE"},
        {"A.CREATE": "Alpha", "B.CREATE": "Beta/Gamma"},
        {"Alpha"},
        {"Gamma"},
    )
    assert missing == [("B.CREATE", "Beta")]


def test_missing_resource_schemas_all_exempted_is_empty() -> None:
    assert cc.missing_resource_schemas(
        {"A.CREATE"}, {"A.CREATE": "Alpha/Beta"}, set(), {"Alpha", "Beta"}
    ) == []


def test_check_resource_schemas_passes_on_current_contract() -> None:
    """当前台账（12 个已实现 + 1 条豁免）应通过。

    豁免只剩 ScriptLock：Project / Episode / ScriptRevision 已进 object_schemas，
    旧豁免属于「冗余豁免」，闸门现在会直接报红（审查 N4）。
    """
    assert cc.check_implemented_resource_schemas() is True
    assert set(cc._schema_exemptions()) == {"ScriptLock"}


def test_check_resource_schemas_fails_for_redundant_exemption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """对象已有 schema 却仍登记豁免 = 冗余豁免，必须报红（审查 N4）。"""
    exempt = tmp_path / "exemptions.yaml"
    exempt.write_text(
        "exemptions:\n  Project: \"未进入 object_schemas\"\n", encoding="utf-8"
    )
    monkeypatch.setattr(cc, "RESOURCE_EXEMPTIONS_PATH", exempt)
    assert cc.check_implemented_resource_schemas() is False


def test_check_resource_schemas_fails_for_unimplemented_object(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """把 ShotDraft（object_schemas 里没有定义）伪造成已实现 → 必须报红。"""
    ledger = tmp_path / "ledger.yaml"
    ledger.write_text(
        "implemented:\n"
        "  SHOT.DRAFT.CREATE:\n"
        "    route: POST /shots/drafts\n"
        "not_implemented: {}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(cc, "LEDGER_PATH", ledger)
    assert cc.check_implemented_resource_schemas() is False


def test_check_resource_schemas_fails_for_orphan_exemption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """豁免了没有任何已实现动作引用的类型 = 过期条目，必须报红。"""
    exempt = tmp_path / "exemptions.yaml"
    exempt.write_text(
        "exemptions:\n  NotReferencedAnywhere: \"过期条目\"\n", encoding="utf-8"
    )
    monkeypatch.setattr(cc, "RESOURCE_EXEMPTIONS_PATH", exempt)
    assert cc.check_implemented_resource_schemas() is False


# --------------------------------------------------------------------------- #
# 状态可达性闸门（静默不可达 = 契约债务必须显式登记）
# --------------------------------------------------------------------------- #
def test_contract_state_names_extracts_asset_states() -> None:
    text = (
        "GENERATED→TECHNICAL_QC→HUMAN_REVIEW→APPROVED→GOLD；"
        "可进入 REJECTED / QUARANTINED / REWORK / SUPERSEDED / ARCHIVED"
    )
    assert cc._contract_state_names(text) == {
        "GENERATED",
        "TECHNICAL_QC",
        "HUMAN_REVIEW",
        "APPROVED",
        "GOLD",
        "REJECTED",
        "QUARANTINED",
        "REWORK",
        "SUPERSEDED",
        "ARCHIVED",
    }


def test_state_reachability_passes_on_current_contract() -> None:
    assert cc.check_state_reachability() is True


def test_state_reachability_detects_undeclared_unreachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """把债务登记清空 → 5 个旁支状态变成静默不可达，必须报红。"""
    gaps = tmp_path / "gaps.yaml"
    gaps.write_text("gaps: {}\n", encoding="utf-8")
    monkeypatch.setattr(cc, "STATE_GAPS_PATH", gaps)
    assert cc.check_state_reachability() is False


def test_state_reachability_detects_stale_debt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """把可达状态误登记为缺口（陈旧债务）→ 必须报红。"""
    import yaml

    data = yaml.safe_load(cc.STATE_GAPS_PATH.read_text(encoding="utf-8"))
    data["gaps"]["AssetVersion"]["GENERATED"] = {
        "reason": "陈旧登记",
        "contract_ref": "x",
        "unblocks_when": "y",
    }
    gaps = tmp_path / "gaps.yaml"
    gaps.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    monkeypatch.setattr(cc, "STATE_GAPS_PATH", gaps)
    assert cc.check_state_reachability() is False


def test_state_reachability_requires_gap_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """债务登记缺 unblocks_when → 必须报红（债务不能含糊）。"""
    import yaml

    data = yaml.safe_load(cc.STATE_GAPS_PATH.read_text(encoding="utf-8"))
    del data["gaps"]["AssetVersion"]["REJECTED"]["unblocks_when"]
    gaps = tmp_path / "gaps.yaml"
    gaps.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    monkeypatch.setattr(cc, "STATE_GAPS_PATH", gaps)
    assert cc.check_state_reachability() is False


# --------------------------------------------------------------------------- #
# 全部 ACTIVE action 的对象 schema 覆盖闸门（缺口必须显式登记）
# --------------------------------------------------------------------------- #
def _write_gaps(tmp_path: Path, gaps: dict) -> Path:
    import yaml

    path = tmp_path / "resource_gaps.yaml"
    path.write_text(yaml.safe_dump({"gaps": gaps}, allow_unicode=True), encoding="utf-8")
    return path


def test_active_resource_schemas_passes_on_current_contract() -> None:
    """当前 9 项债务登记完整，应通过。"""
    assert cc.check_active_resource_schemas() is True


def test_active_schema_detects_undeclared_gap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """清空债务 → 9 个 ACTIVE 资源类型变成静默缺口，必须报红。"""
    monkeypatch.setattr(cc, "RESOURCE_GAPS_PATH", _write_gaps(tmp_path, {}))
    assert cc.check_active_resource_schemas() is False


def test_active_schema_detects_stale_debt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """已补 schema 的类型仍挂债务 = 陈旧债务，必须报红。"""
    monkeypatch.setattr(
        cc,
        "RESOURCE_GAPS_PATH",
        _write_gaps(
            tmp_path,
            {
                "Project": {
                    "referenced_by": ["PROJECT.CREATE"],
                    "reason": "陈旧登记",
                    "contract_ref": "x",
                    "unblocks_when": "y",
                }
            },
        ),
    )
    assert cc.check_active_resource_schemas() is False


def test_active_schema_detects_orphan_debt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """债务类型没有任何 ACTIVE 动作引用 = 孤儿债务，必须报红。"""
    monkeypatch.setattr(
        cc,
        "RESOURCE_GAPS_PATH",
        _write_gaps(
            tmp_path,
            {
                "PhantomObject": {
                    "referenced_by": ["PHANTOM.CREATE"],
                    "reason": "假缺口",
                    "contract_ref": "x",
                    "unblocks_when": "y",
                }
            },
        ),
    )
    assert cc.check_active_resource_schemas() is False


def test_active_schema_requires_gap_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """债务缺 reason → 必须报红（债务不能含糊）。"""
    entry = {
        "referenced_by": ["SHOT.DRAFT.CREATE"],
        "reason": "",
        "contract_ref": "x",
        "unblocks_when": "y",
    }
    monkeypatch.setattr(cc, "RESOURCE_GAPS_PATH", _write_gaps(tmp_path, {"ShotDraft": entry}))
    assert cc.check_active_resource_schemas() is False


def test_active_schema_detects_wrong_referenced_by(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """referenced_by 与契约实际引用不一致 → 必须报红（台账不能撒谎）。"""
    entry = {
        "referenced_by": ["SHOT.DRAFT.CREATE", "SHOT.DRAFT.CONFIRM"],
        "reason": "r",
        "contract_ref": "x",
        "unblocks_when": "y",
    }
    monkeypatch.setattr(cc, "RESOURCE_GAPS_PATH", _write_gaps(tmp_path, {"ShotDraft": entry}))
    assert cc.check_active_resource_schemas() is False


# --------------------------------------------------------------------------- #
# 事件契约校验（check_event_catalog）
# --------------------------------------------------------------------------- #
def _event_entry(name: str, emitted_by: str = "service.py") -> dict[str, object]:
    return {
        "event_type": name,
        "aggregate_type": "DemoAggregate",
        "emitted_by": emitted_by,
        "payload_required": ["tenant_id"],
        "contract_ref": "contracts/schemas/object_schemas.schema.json#Demo",
    }


def _event_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    emitted: list[str],
    entries: list[dict[str, object]],
    gaps: dict[str, dict[str, str]] | None = None,
    implemented: list[str] | None = None,
    audit_events: dict[str, str] | None = None,
) -> None:
    code = tmp_path / "code"
    code.mkdir()
    (code / "service.py").write_text(
        "\n".join(f'event_type="{name}"' for name in emitted) + "\n",
        encoding="utf-8",
    )
    catalog = tmp_path / "event_catalog.yaml"
    catalog.write_text(
        yaml.safe_dump({"version": 1, "events": entries}, allow_unicode=True),
        encoding="utf-8",
    )
    gaps_path = tmp_path / "event_gaps.yaml"
    gaps_path.write_text(
        yaml.safe_dump({"version": 1, "gaps": gaps or {}}, allow_unicode=True),
        encoding="utf-8",
    )
    registry = tmp_path / "action_registry.yaml"
    registry.write_text(
        yaml.safe_dump(
            {
                "actions": [
                    {"action_key": key, "audit_event": event}
                    for key, event in (audit_events or {}).items()
                ]
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    ledger = tmp_path / "ledger.yaml"
    ledger.write_text(
        yaml.safe_dump(
            {
                "implemented": {
                    key: {"route": "POST /demo", "module": "m.py"}
                    for key in (implemented or [])
                }
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(cc, "EVENT_SCAN_ROOTS", (code,))
    monkeypatch.setattr(cc, "EVENT_CATALOG_PATH", catalog)
    monkeypatch.setattr(cc, "EVENT_GAPS_PATH", gaps_path)
    monkeypatch.setattr(cc, "ACTION_REGISTRY_PATH", registry)
    monkeypatch.setattr(cc, "LEDGER_PATH", ledger)


def test_event_catalog_passes_when_consistent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _event_fixture(
        tmp_path,
        monkeypatch,
        emitted=["DEMO_EVENT"],
        entries=[_event_entry("DEMO_EVENT")],
    )
    assert cc.check_event_catalog() is True


def test_event_catalog_detects_undeclared_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """代码里发了个没登记的事件 = 私自发契约，必须报红。"""
    _event_fixture(tmp_path, monkeypatch, emitted=["SNEAKY_EVENT"], entries=[])
    assert cc.check_event_catalog() is False


def test_event_catalog_detects_stale_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """登记了但代码已不再发 = 陈旧台账，必须报红。"""
    _event_fixture(
        tmp_path,
        monkeypatch,
        emitted=[],
        entries=[_event_entry("GHOST_EVENT")],
    )
    assert cc.check_event_catalog() is False


def test_event_catalog_detects_emitted_by_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """emitted_by 指向别的文件 = 落点漂移，必须报红。"""
    _event_fixture(
        tmp_path,
        monkeypatch,
        emitted=["DEMO_EVENT"],
        entries=[_event_entry("DEMO_EVENT", emitted_by="other.py")],
    )
    assert cc.check_event_catalog() is False


def test_event_catalog_requires_gap_for_missing_action_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """动作声明了 audit_event 却不发、也没登记缺口 → 必须报红（不许静默）。"""
    _event_fixture(
        tmp_path,
        monkeypatch,
        emitted=[],
        entries=[],
        implemented=["DEMO.CREATE"],
        audit_events={"DEMO.CREATE": "DEMO_CREATED"},
    )
    assert cc.check_event_catalog() is False


# --------------------------------------------------------------------------- #
# Schema 结构 + 运行时字段一致性（check_schema_objects）
# --------------------------------------------------------------------------- #
def _write_schema(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, definitions: dict) -> None:
    path = tmp_path / "object_schemas.json"
    path.write_text(
        json.dumps({"definitions": definitions}, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(cc, "SCHEMA_PATH", path)


def test_schema_rejects_empty_properties(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_schema(tmp_path, monkeypatch, {"Demo": {"type": "object", "properties": {}}})
    assert cc.check_schema_objects() is False


def test_schema_rejects_required_not_in_properties(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_schema(
        tmp_path,
        monkeypatch,
        {
            "Demo": {
                "type": "object",
                "properties": {"a": {"type": "string"}},
                "required": ["a", "phantom"],
            }
        },
    )
    assert cc.check_schema_objects() is False


def test_schema_rejects_dangling_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_schema(
        tmp_path,
        monkeypatch,
        {
            "Demo": {
                "type": "object",
                "properties": {"child": {"$ref": "#/definitions/Ghost"}},
            }
        },
    )
    assert cc.check_schema_objects() is False


def test_schema_rejects_runtime_extra_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """响应出现契约未声明的字段 = 字段漂移，必须报红（P2-4 的根因）。"""

    class _ExtraKeyProbe:
        def to_dict(self) -> dict:
            return {
                "entity_id": "x",
                "entity_type": "AssetVersion",
                "schema_version": "1.0",
                "entity_version": 1,
                "status": "GENERATED",
                "owner_module": "asset-service",
                "created_at": "2026-01-01T00:00:00+00:00",
                "created_by": "probe",
                "updated_at": "2026-01-01T00:00:00+00:00",
                "audit_ref": "probe",
                "private_note": "leak",
            }

    real = cc._serializer_factories()

    def _fake_factories() -> dict[str, object]:
        return {**real, "AssetVersion": _ExtraKeyProbe()}

    monkeypatch.setattr(cc, "_serializer_factories", _fake_factories)
    assert cc.check_schema_objects() is False


# --------------------------------------------------------------------------- #
# load_yaml_mapping 判空/语法损坏 fail-fast（T1）
# --------------------------------------------------------------------------- #
def test_load_yaml_mapping_empty_file(tmp_path: Path) -> None:
    """空文件 → ValueError（fail-fast，不静默返回空 dict）。"""
    empty = tmp_path / "empty.yaml"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(ValueError) as exc_info:
        cc.load_yaml_mapping(empty)
    assert "解析结果不是映射" in str(exc_info.value)


def test_load_yaml_mapping_non_mapping(tmp_path: Path) -> None:
    """顶层是 list（而非映射）→ ValueError。"""
    list_file = tmp_path / "list.yaml"
    list_file.write_text("- a\n- b\n", encoding="utf-8")
    with pytest.raises(ValueError) as exc_info:
        cc.load_yaml_mapping(list_file)
    assert "解析结果不是映射" in str(exc_info.value)


def test_load_yaml_mapping_syntax_error(tmp_path: Path) -> None:
    """语法损坏的 YAML → ValueError（捕获 YAMLError 统一转，而非 traceback）。"""
    bad = tmp_path / "bad.yaml"
    bad.write_text("key: [unclosed\n  nested: *broken\n", encoding="utf-8")
    with pytest.raises(ValueError) as exc_info:
        cc.load_yaml_mapping(bad)
    assert "YAML 解析失败" in str(exc_info.value)


def test_load_yaml_mapping_error_has_full_path(tmp_path: Path) -> None:
    """错误信息携带完整路径（R8：同名文件也能定位）。"""
    empty = tmp_path / "some_gaps.yaml"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(ValueError) as exc_info:
        cc.load_yaml_mapping(empty)
    assert str(empty) in str(exc_info.value)


# --------------------------------------------------------------------------- #
# 转移入口债务闸门（审查 N3：删条目/删文件/坏形状都不能变绿）
# --------------------------------------------------------------------------- #
def _gaps_without(*objects: str, tmp_path: Path) -> Path:
    """基于真实债务文件，摘掉指定对象的 transition_entry_gaps 条目。"""
    data = yaml.safe_load(cc.STATE_GAPS_PATH.read_text(encoding="utf-8"))
    for name in objects:
        data["transition_entry_gaps"].pop(name, None)
    target = tmp_path / "gaps.yaml"
    target.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return target


def test_transition_entry_gaps_passes_on_current_contract() -> None:
    assert cc.check_transition_entry_gaps() is True


def test_transition_entry_gaps_detects_dropped_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """删掉 Episode 的债务条目 → 反向完备性检查必须报红（不能打印 PASS）。"""
    monkeypatch.setattr(cc, "STATE_GAPS_PATH", _gaps_without("Episode", tmp_path=tmp_path))
    assert cc.check_transition_entry_gaps() is False


def test_transition_entry_gaps_detects_missing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """删掉整个债务文件 → 已知无入口的对象必须让闸门报红。"""
    monkeypatch.setattr(cc, "STATE_GAPS_PATH", tmp_path / "does-not-exist.yaml")
    assert cc.check_transition_entry_gaps() is False


def test_transition_entry_gaps_bad_shape_raises_value_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """债务详情写成字符串 → ValueError（结构化 FAIL），而不是 AttributeError 崩溃。"""
    bad = tmp_path / "gaps.yaml"
    bad.write_text(
        "transition_entry_gaps:\n  Project: oops\n", encoding="utf-8"
    )
    monkeypatch.setattr(cc, "STATE_GAPS_PATH", bad)
    with pytest.raises(ValueError) as exc_info:
        cc.check_transition_entry_gaps()
    assert "transition_entry_gaps.Project" in str(exc_info.value)


def test_transition_entry_gaps_detects_stale_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AssetVersion 已有已实现的状态变更 action，再登记债务即陈旧 → 报红。"""
    data = yaml.safe_load(cc.STATE_GAPS_PATH.read_text(encoding="utf-8"))
    data["transition_entry_gaps"]["AssetVersion"] = {
        "reason": "陈旧登记",
        "contract_ref": "x",
        "unblocks_when": "y",
    }
    target = tmp_path / "gaps.yaml"
    target.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    monkeypatch.setattr(cc, "STATE_GAPS_PATH", target)
    assert cc.check_transition_entry_gaps() is False


def test_transition_entry_gaps_contract_registration_is_not_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """契约先登记、运行时未实现不算陈旧（否则会逼着人删掉真实缺口）。

    「已补上入口」只认运行时台账 implemented；把它伪造成 registry 里已有
    PROJECT.STATUS.CHANGE（但未实现），债务条目必须仍然合法。
    """
    original = cc._status_change_action_keys

    def _with_declared_status_change(resource_type: str) -> set[str]:
        keys = original(resource_type)
        if resource_type == "Project":
            keys = keys | {"PROJECT.STATUS.CHANGE"}
        return keys

    monkeypatch.setattr(cc, "_status_change_action_keys", _with_declared_status_change)
    assert cc.check_transition_entry_gaps() is True
