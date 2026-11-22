"""契约一致性校验脚本测试。

覆盖：
1. check_project_state_machine 正向：当前契约与实现一致，应通过。
2. 负向：临时制造不一致（契约多/少转移），应被检出。
3. 状态名只能来自结构化的 allowed_states，不再从文本解析。
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
    # v2：契约转移已结构化，注入点从「解析文本」改成「读条目」
    monkeypatch.setattr(cc, "contract_edges", lambda entry: fake_transitions)
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
    # v2：契约转移已结构化，注入点从「解析文本」改成「读条目」
    monkeypatch.setattr(cc, "contract_edges", lambda entry: fake_transitions)
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
def test_state_machine_states_reads_allowed_states() -> None:
    """允许状态集合直接取 allowed_states：旁支状态不再靠正则从备注里捞。"""
    states = cc.state_machine_states(
        [{"object": "AssetVersion", "allowed_states": ["GENERATED", "QUARANTINED"]}]
    )
    assert states == {"AssetVersion": {"GENERATED", "QUARANTINED"}}


def test_state_machine_states_rejects_missing_allowed_states() -> None:
    """没有 allowed_states 的条目是契约损坏，不能「当作空集」继续往下跑。"""
    with pytest.raises(ValueError, match="allowed_states"):
        cc.state_machine_states([{"object": "AssetVersion"}])


def test_state_machine_states_rejects_duplicate_states() -> None:
    with pytest.raises(ValueError, match="重复"):
        cc.state_machine_states(
            [{"object": "AssetVersion", "allowed_states": ["GENERATED", "GENERATED"]}]
        )


def test_state_machine_states_rejects_non_state_token() -> None:
    """「旧版本」「可进入」这类措辞曾被文本解析当成状态值，必须在入口拦住。"""
    with pytest.raises(ValueError, match="非状态值"):
        cc.state_machine_states(
            [{"object": "TimelineDraft", "allowed_states": ["DRAFT", "旧版本"]}]
        )


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
    """删掉无入口边的债务条目 → 反向完备性必须报红（不能打印 PASS）。

    R16 之后 ``CONSTRAINT.PACK.STATUS.CHANGE`` 已实现，真实缺口清零，本用例改为
    自足构造：人为把该动作从运行时 implemented 台账摘掉，令其拥有的三条治理边
    重新成为「可达但无入口」，再删掉其债务登记，验证「无登记 = 红」。
    """
    original_impl = cc._implemented_action_keys
    monkeypatch.setattr(
        cc,
        "_implemented_action_keys",
        lambda: original_impl() - {"CONSTRAINT.PACK.STATUS.CHANGE"},
    )
    monkeypatch.setattr(
        cc, "STATE_GAPS_PATH", _gaps_without("ConstraintPack", tmp_path=tmp_path)
    )
    assert cc.check_transition_entry_gaps() is False


def test_transition_entry_gaps_detects_missing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """删掉整个债务文件、且仍存在无入口的边 → 必须报红。

    R16 后所有真实边均已实现，单删文件不会产生缺口；这里人为把
    ``CONSTRAINT.PACK.LOCK`` 从 implemented 台账摘掉，令 ``DRAFT→ACTIVE`` 重新
    成为无入口的边，再指向不存在的债务文件，验证「没有文件 ≠ 没有债务」。
    """
    original_impl = cc._implemented_action_keys
    monkeypatch.setattr(
        cc,
        "_implemented_action_keys",
        lambda: original_impl() - {"CONSTRAINT.PACK.LOCK"},
    )
    monkeypatch.setattr(cc, "STATE_GAPS_PATH", tmp_path / "does-not-exist.yaml")
    assert cc.check_transition_entry_gaps() is False


def test_transition_entry_gaps_bad_shape_raises_value_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """债务详情写成字符串 → ValueError（结构化 FAIL），而不是 AttributeError 崩溃。"""
    bad = tmp_path / "gaps.yaml"
    bad.write_text(
        "transition_entry_gaps:\n  Project: oops\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(cc, "STATE_GAPS_PATH", bad)
    with pytest.raises(ValueError) as exc_info:
        cc.check_transition_entry_gaps()
    assert "transition_entry_gaps.Project" in str(exc_info.value)


def test_transition_entry_gaps_bad_edge_detail_raises_value_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """边这一层的详情不是映射 → ValueError，而不是在 `.get` 上崩掉。

    两层嵌套之后多了一种坏形状（对象对了、边对了、详情不是映射），必须单独覆盖：
    只在顶层做形状校验会让它在 detail.get() 处抛 AttributeError，闸门带着
    traceback 崩掉而不是给出可读的 FAIL。
    """
    bad = tmp_path / "gaps.yaml"
    bad.write_text(
        "transition_entry_gaps:\n  ConstraintPack:\n\n    'ACTIVE -> REVOKED': oops\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(cc, "STATE_GAPS_PATH", bad)
    with pytest.raises(ValueError) as exc_info:
        cc.check_transition_entry_gaps()
    assert "transition_entry_gaps.ConstraintPack.ACTIVE -> REVOKED" in str(
        exc_info.value
    )


def test_transition_entry_gaps_rejects_unknown_edge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """登记一条不存在的边 → 报红。

    手抄边标签是本判据唯一的写入风险：写错一个状态名不会撞上任何约束，只会让
    「这条边已登记」看起来成立，而真实的缺口仍然没人登记。
    """
    data = yaml.safe_load(cc.STATE_GAPS_PATH.read_text(encoding="utf-8"))
    data["transition_entry_gaps"]["ConstraintPack"]["DRAFT -> LOCKED"] = {
        "reason": "手抄错边",
        "contract_ref": "x",
        "unblocks_when": "y",
    }
    target = tmp_path / "gaps.yaml"
    target.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    monkeypatch.setattr(cc, "STATE_GAPS_PATH", target)
    assert cc.check_transition_entry_gaps() is False


def test_transition_entry_gaps_detects_stale_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """给一条 owner_action 已实现的边登记债务 → 陈旧，必须报红。"""
    data = yaml.safe_load(cc.STATE_GAPS_PATH.read_text(encoding="utf-8"))
    data["transition_entry_gaps"]["AssetVersion"] = {
        "GENERATED -> TECHNICAL_QC": {
            "reason": "陈旧登记",
            "contract_ref": "x",
            "unblocks_when": "y",
        }
    }
    target = tmp_path / "gaps.yaml"
    target.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    monkeypatch.setattr(cc, "STATE_GAPS_PATH", target)
    assert cc.check_transition_entry_gaps() is False


def test_transition_entry_gaps_contract_registration_is_not_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """契约先登记、运行时未实现不算陈旧（否则会逼着人删掉真实缺口）。

    「已补上入口」只认运行时台账 implemented，不认 action_registry 的登记。
    构造：把 CONSTRAINT.PACK.LOCK 从 implemented 摘掉（模拟「契约已登记、运行时
    尚未实现」），并把 DRAFT→ACTIVE 也登记进债务——此时四条边全部「未实现且有
    登记」，闸门应 PASS；若判据改成按 registry 判定，这里会误报「陈旧」。
    """
    original_impl = cc._implemented_action_keys

    def _without_pack_lock() -> set[str]:
        return original_impl() - {"CONSTRAINT.PACK.LOCK"}

    data = yaml.safe_load(cc.STATE_GAPS_PATH.read_text(encoding="utf-8"))
    data["transition_entry_gaps"]["ConstraintPack"]["DRAFT -> ACTIVE"] = {
        "reason": "契约已登记 CONSTRAINT.PACK.LOCK，但运行时入口尚未实现",
        "contract_ref": "contracts/action_registry/action_registry.yaml",
        "unblocks_when": "实现 service/route 并登记 action_implementations.yaml",
    }
    target = tmp_path / "gaps.yaml"
    target.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")

    monkeypatch.setattr(cc, "_implemented_action_keys", _without_pack_lock)
    monkeypatch.setattr(cc, "STATE_GAPS_PATH", target)
    assert cc.check_transition_entry_gaps() is True


# --------------------------------------------------------------------------- #
# 安全与数据边界契约（check_security_boundary）
# --------------------------------------------------------------------------- #
def test_security_boundary_rejects_handwritten_satisfied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """写 status: satisfied 必须报红（本轮整改的核心：状态不可手写）。

    这是「宣称即销项」的最后一道防线：只要 satisfied 还能被编辑，闸门就永远
    只能证明「有人写了 satisfied」，而不能证明事实成立。
    """
    data = yaml.safe_load(cc.SECURITY_BOUNDARY_PATH.read_text(encoding="utf-8"))
    first = data["authorization"]["invariants"][0]
    first.pop("partial", None)
    first["status"] = "satisfied"
    target = tmp_path / "security_boundary.yaml"
    target.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    monkeypatch.setattr(cc, "SECURITY_BOUNDARY_PATH", target)
    assert cc.check_security_boundary() is False


def test_security_boundary_rejects_partial_with_debt_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同一条不变量既 partial 又 debt 必须报红（欠账形态必须唯一）。

    构造而非取材：本用例自己把一条断言条目改写成手写 partial，再叠加 debt 子块。
    旧版从当前契约里「找一个真实 partial」，欠账清零那天就会以 StopIteration
    失败——测试不该依赖被整改对象的状态，否则它测的是「仓库现在长什么样」，
    不是「闸门会不会拦」。
    """
    data = yaml.safe_load(cc.SECURITY_BOUNDARY_PATH.read_text(encoding="utf-8"))
    target_inv = data["authorization"]["invariants"][0]
    target_inv.pop("assertion", None)
    target_inv.pop("runtime_evidence", None)
    target_inv["status"] = "partial"
    target_inv["partial"] = {
        "satisfied": "构造用例：假装已满足的部分",
        "missing": "构造用例：假装未完成的部分",
        "unblocks_when": "应只保留一种欠账形态",
    }
    target_inv["debt"] = {
        "reason": "陈旧债务：与 partial 子块并存",
        "contract_ref": "tests/unit/test_contract_check.py",
        "unblocks_when": "应只保留一种欠账形态",
    }
    target = tmp_path / "security_boundary.yaml"
    target.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    monkeypatch.setattr(cc, "SECURITY_BOUNDARY_PATH", target)
    assert cc.check_security_boundary() is False


def test_security_boundary_rejects_unregistered_assertion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """引用未注册的断言 id 必须报红（防止写个不存在的判据混过去）。"""
    data = yaml.safe_load(cc.SECURITY_BOUNDARY_PATH.read_text(encoding="utf-8"))
    data["authorization"]["invariants"][1]["assertion"] = "auth.does_not_exist"
    target = tmp_path / "security_boundary.yaml"
    target.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    monkeypatch.setattr(cc, "SECURITY_BOUNDARY_PATH", target)
    assert cc.check_security_boundary() is False


def test_security_boundary_rejects_failing_assertion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """断言执行不通过必须报红（实现回退时闸门要真的拦下来）。"""
    import boundary_assertions as ba

    monkeypatch.setitem(
        ba._REGISTRY,
        "auth.rbac_capability_enforced",
        lambda: (False, "注入的失败断言"),
    )
    data = yaml.safe_load(cc.SECURITY_BOUNDARY_PATH.read_text(encoding="utf-8"))
    target = tmp_path / "security_boundary.yaml"
    target.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    monkeypatch.setattr(cc, "SECURITY_BOUNDARY_PATH", target)
    assert cc.check_security_boundary() is False


def test_security_boundary_rejects_missing_assertion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """既无 status 又无 assertion 必须报红（不变量不能没有可执行判据）。"""
    data = yaml.safe_load(cc.SECURITY_BOUNDARY_PATH.read_text(encoding="utf-8"))
    data["authorization"]["invariants"][1].pop("assertion")
    target = tmp_path / "security_boundary.yaml"
    target.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    monkeypatch.setattr(cc, "SECURITY_BOUNDARY_PATH", target)
    assert cc.check_security_boundary() is False


def test_security_boundary_rejects_dead_runtime_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """runtime_evidence 指向不存在的测试必须报红（证据不能指向空气）。"""
    data = yaml.safe_load(cc.SECURITY_BOUNDARY_PATH.read_text(encoding="utf-8"))
    data["authorization"]["invariants"][1]["runtime_evidence"] = [
        "tests/unit/test_authorize.py::test_this_does_not_exist"
    ]
    target = tmp_path / "security_boundary.yaml"
    target.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    monkeypatch.setattr(cc, "SECURITY_BOUNDARY_PATH", target)
    assert cc.check_security_boundary() is False


def test_security_boundary_assertion_registry_is_not_empty() -> None:
    """断言注册表必须真的有内容（否则所有不变量都会因缺判据而报红）。"""
    import boundary_assertions as ba

    assert len(ba.registered_ids()) >= 10


def test_check_security_boundary_passes_on_current_contract() -> None:
    """当前 security_boundary.yaml 结构自洽且断言全部通过。"""
    assert cc.check_security_boundary() is True


def test_security_boundary_detects_missing_required_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """不变量缺 fact_source 字段应被检出（债务不能含糊）。"""
    data = yaml.safe_load(cc.SECURITY_BOUNDARY_PATH.read_text(encoding="utf-8"))
    # 删除第一条不变量的 fact_source。
    del data["authorization"]["invariants"][0]["fact_source"]
    target = tmp_path / "security_boundary.yaml"
    target.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    monkeypatch.setattr(cc, "SECURITY_BOUNDARY_PATH", target)
    assert cc.check_security_boundary() is False


def test_security_boundary_detects_invalid_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """非法 status 值应被检出（三态之外一律报红）。"""
    data = yaml.safe_load(cc.SECURITY_BOUNDARY_PATH.read_text(encoding="utf-8"))
    data["authorization"]["invariants"][0]["status"] = "maybe"
    target = tmp_path / "security_boundary.yaml"
    target.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    monkeypatch.setattr(cc, "SECURITY_BOUNDARY_PATH", target)
    assert cc.check_security_boundary() is False


def test_security_boundary_detects_debt_missing_unblocks_when(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """debt 缺 unblocks_when 字段应被检出（债务不能永久豁免）。"""
    data = yaml.safe_load(cc.SECURITY_BOUNDARY_PATH.read_text(encoding="utf-8"))
    # 主动把第一条不变量改成 debt（含 reason/contract_ref，但缺 unblocks_when）。
    # 当前真实契约的 partial/debt 条目都不是 authorization 首条，不能靠「找一条」
    # 来构造负例，因此显式注入。
    first = data["authorization"]["invariants"][0]
    first.pop("partial", None)
    first["status"] = "debt"
    first["debt"] = {
        "reason": "测试注入的债务（缺 unblocks_when）",
        "contract_ref": "tests/unit/test_contract_check.py",
    }
    target = tmp_path / "security_boundary.yaml"
    target.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    monkeypatch.setattr(cc, "SECURITY_BOUNDARY_PATH", target)
    assert cc.check_security_boundary() is False


def test_security_boundary_detects_duplicate_invariant_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """重复不变量 id 应被检出（防复制粘贴出重名）。"""
    data = yaml.safe_load(cc.SECURITY_BOUNDARY_PATH.read_text(encoding="utf-8"))
    first = data["authorization"]["invariants"][0]
    data["authorization"]["invariants"].append(dict(first))
    target = tmp_path / "security_boundary.yaml"
    target.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    monkeypatch.setattr(cc, "SECURITY_BOUNDARY_PATH", target)
    assert cc.check_security_boundary() is False


# --------------------------------------------------------------------------- #
# 提案状态（check_proposal_status，P2-04）
# --------------------------------------------------------------------------- #
def _write_proposal(dir_path: Path, name: str, front_matter: str, body: str = "# 提案\n") -> None:
    dir_path.mkdir(parents=True, exist_ok=True)
    (dir_path / name).write_text(
        f"---\n{front_matter.strip()}\n---\n\n{body}", encoding="utf-8"
    )


def test_proposal_status_passes_on_repository() -> None:
    """仓库里的提案必须全部自洽（状态与未完成项一致、事实源真实存在）。"""
    assert cc.check_proposal_status() is True


def test_proposal_without_front_matter_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """缺 front-matter → 报红（状态不可手写在正文里）。"""
    (tmp_path / "p.md").write_text("# 提案\n\n状态：已完成\n", encoding="utf-8")
    monkeypatch.setattr(cc, "PROPOSALS_DIR", tmp_path)
    assert cc.check_proposal_status() is False


def test_proposal_implemented_with_open_items_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """声明 implemented 却留着未完成项 → 报红（勾完成与留债不能并存）。"""
    _write_proposal(
        tmp_path,
        "p.md",
        """
doc_type: proposal
status: implemented
authoritative_sources: [contracts/security_boundary.yaml]
open_items:
  - item: "还有一件没做完"
    source: contracts/security_boundary.yaml
""",
    )
    monkeypatch.setattr(cc, "PROPOSALS_DIR", tmp_path)
    assert cc.check_proposal_status() is False


def test_proposal_in_progress_without_open_items_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """声明 in_progress 却说不清差什么 → 报红（这就是 P2-04 的腐化形态）。"""
    _write_proposal(
        tmp_path,
        "p.md",
        """
doc_type: proposal
status: in_progress
authoritative_sources: [contracts/security_boundary.yaml]
open_items: []
""",
    )
    monkeypatch.setattr(cc, "PROPOSALS_DIR", tmp_path)
    assert cc.check_proposal_status() is False


def test_proposal_source_pointing_to_air_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """事实源指向不存在的文件 → 报红（防"引用空气"）。"""
    _write_proposal(
        tmp_path,
        "p.md",
        """
doc_type: proposal
status: implemented
authoritative_sources: [contracts/does_not_exist.yaml]
open_items: []
""",
    )
    monkeypatch.setattr(cc, "PROPOSALS_DIR", tmp_path)
    assert cc.check_proposal_status() is False


def test_proposal_open_item_anchor_without_debt_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """open item 指向的不变量若没有 residual_risk/debt/partial → 报红。

    这条是 P2-04 的关键：允许写"还差什么"，但必须能在契约里找到对应的债；
    否则"未完成项"又退化成散文。
    """
    _write_proposal(
        tmp_path,
        "p.md",
        """
doc_type: proposal
status: in_progress
authoritative_sources: [contracts/security_boundary.yaml]
open_items:
  - item: "声称还有债"
    source: "contracts/security_boundary.yaml#CRED-1"
""",
    )
    monkeypatch.setattr(cc, "PROPOSALS_DIR", tmp_path)
    assert cc.check_proposal_status() is False


def test_proposal_open_item_anchor_on_debt_invariant_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """指向带 residual_risk 的不变量（AUTH-3）应通过——正向对照。"""
    _write_proposal(
        tmp_path,
        "p.md",
        """
doc_type: proposal
status: in_progress
authoritative_sources: [contracts/security_boundary.yaml]
open_items:
  - item: "双人审批未落地"
    source: "contracts/security_boundary.yaml#AUTH-3"
""",
    )
    monkeypatch.setattr(cc, "PROPOSALS_DIR", tmp_path)
    assert cc.check_proposal_status() is True


def test_proposal_illegal_status_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """status 不在枚举内 → 报红（防"待评审冻结"这类自由文本）。"""
    _write_proposal(
        tmp_path,
        "p.md",
        """
doc_type: proposal
status: 待评审冻结
authoritative_sources: [contracts/security_boundary.yaml]
open_items: []
""",
    )
    monkeypatch.setattr(cc, "PROPOSALS_DIR", tmp_path)
    assert cc.check_proposal_status() is False




# --------------------------------------------------------------------------- #
# check_child_creation_freeze_coverage：有子表引用的容器必须声明冻结
# --------------------------------------------------------------------------- #
def test_freeze_coverage_passes_on_current_tree() -> None:
    assert cc.check_child_creation_freeze_coverage() is True


def test_freeze_coverage_detects_missing_frozen_states(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """把一个容器的冻结声明清空，判据必须变红。

    ``frozen_states`` 默认空元组，「漏声明」与「确实不冻结」运行期同形；
    这条负向用例证明判据真的在看声明，而不是恒真。
    """
    import dataclasses

    from services.project_planning.domain import state_machine

    stripped = dataclasses.replace(state_machine.SCENE_LIFECYCLE, frozen_states=())
    monkeypatch.setattr(state_machine, "SCENE_LIFECYCLE", stripped)

    assert cc.check_child_creation_freeze_coverage() is False


def test_freeze_coverage_detects_stale_exemption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """豁免清单登记一张不存在的表必须报红（陈旧豁免会放过未来的新子表）。"""
    monkeypatch.setitem(
        cc._FREEZE_EXEMPT_PARENTS, "legacy_table_removed_long_ago", "历史遗留"
    )

    assert cc.check_child_creation_freeze_coverage() is False


def test_freeze_coverage_requires_decision_for_unlisted_parent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """有子表引用却两处都没登记 → 必须报红（新增父表得表态）。"""
    monkeypatch.delitem(cc._FREEZE_EXEMPT_PARENTS, "organizations")

    assert cc.check_child_creation_freeze_coverage() is False


# --------------------------------------------------------------------------- #
# check_frozen_state_contract：冻结策略必须是契约事实
# --------------------------------------------------------------------------- #
def _entry_for(lifecycle, **overrides):
    """用实现侧数据构造一个「本来就该通过」的契约条目，再按用例改坏它。"""
    entry = {
        "object": lifecycle.object_name,
        "allowed_states": [state.value for state in lifecycle.states],
        "frozen_states": [state.value for state in lifecycle.frozen_states],
    }
    entry.update(overrides)
    return entry


def test_frozen_state_contract_passes_on_current_tree() -> None:
    assert cc.check_frozen_state_contract() is True


def test_frozen_state_problems_accepts_consistent_entry() -> None:
    from services.project_planning.domain import state_machine

    entry = _entry_for(state_machine.PROJECT_LIFECYCLE)
    assert cc.frozen_state_problems(entry, state_machine.PROJECT_LIFECYCLE) == []


def test_frozen_state_problems_requires_explicit_field() -> None:
    """字段缺失必须报红：默认空元组让「没声明」与「声明为空」运行期同形。"""
    from services.project_planning.domain import state_machine

    entry = _entry_for(state_machine.PROJECT_LIFECYCLE)
    entry.pop("frozen_states")

    problems = cc.frozen_state_problems(entry, state_machine.PROJECT_LIFECYCLE)
    assert any("未声明" in problem for problem in problems), problems


def test_frozen_state_problems_rejects_unknown_state() -> None:
    """冻结一个不在 allowed_states 里的状态 → ``in`` 恒假，必须报红。"""
    from services.project_planning.domain import state_machine

    entry = _entry_for(
        state_machine.PROJECT_LIFECYCLE, frozen_states=["ARCHIVEDD", "ARCHIVED"]
    )

    problems = cc.frozen_state_problems(entry, state_machine.PROJECT_LIFECYCLE)
    assert any("未声明状态" in problem for problem in problems), problems


def test_frozen_state_problems_reports_both_sides_on_drift() -> None:
    """不一致时要能看出是哪一侧多——只报「不一致」等于让人猜。"""
    from services.project_planning.domain import state_machine

    entry = _entry_for(state_machine.PROJECT_LIFECYCLE, frozen_states=["ARCHIVED"])

    problems = cc.frozen_state_problems(entry, state_machine.PROJECT_LIFECYCLE)
    drift = [problem for problem in problems if "不一致" in problem]
    assert drift, problems
    assert "实现多 ['COMPLETED']" in drift[0], drift[0]


def test_frozen_state_contract_detects_impl_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """实现侧悄悄改冻结集合（契约未动）→ 判据必须变红。

    这正是 A19 residual 指出的形态：改一行元组就能让归档项目重新接受新集数，
    而冻结覆盖判据仍然全绿（它只问「有没有声明」）。
    """
    import dataclasses

    from services.project_planning.domain import state_machine

    real, problems = cc._implemented_lifecycles()
    assert problems == []

    drifted = dict(real)
    drifted["Project"] = dataclasses.replace(
        state_machine.PROJECT_LIFECYCLE,
        frozen_states=(state_machine.ProjectStatus.ARCHIVED,),
    )
    monkeypatch.setattr(cc, "_implemented_lifecycles", lambda: (drifted, []))

    assert cc.check_frozen_state_contract() is False


def test_frozen_state_contract_detects_missing_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """已实现状态机找不到 Lifecycle → 冻结策略无处声明，必须报红。"""
    real, problems = cc._implemented_lifecycles()
    assert problems == []

    incomplete = {key: value for key, value in real.items() if key != "Project"}
    monkeypatch.setattr(cc, "_implemented_lifecycles", lambda: (incomplete, []))

    assert cc.check_frozen_state_contract() is False


def test_frozen_state_contract_detects_unregistered_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """有 Lifecycle 却不在 _STATE_MACHINE_SOURCES 里 → 转移与冻结都不被比对，报红。"""
    import dataclasses

    from services.project_planning.domain import state_machine

    real, problems = cc._implemented_lifecycles()
    assert problems == []

    extra = dict(real)
    extra["Ghost"] = dataclasses.replace(
        state_machine.PROJECT_LIFECYCLE, object_name="Ghost", frozen_states=()
    )
    monkeypatch.setattr(cc, "_implemented_lifecycles", lambda: (extra, []))

    assert cc.check_frozen_state_contract() is False
# --------------------------------------------------------------------------- #
# Gate C：阶段表派生一致性（phase_map 不得成为第二份权威）
# --------------------------------------------------------------------------- #
def _phase_fixture(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """复制契约与基线到临时目录，避免测试改动真实冻结产物。"""
    import shutil

    contracts = tmp_path / "contracts"
    shutil.copytree(cc.CONTRACTS, contracts)
    baseline = tmp_path / "baseline.md"
    shutil.copyfile(cc.BASELINE_DOC, baseline)
    monkeypatch.setattr(cc, "CONTRACTS", contracts)
    monkeypatch.setattr(cc, "BASELINE_DOC", baseline)
    return contracts / "phases" / "phase_map.yaml"


def test_phase_map_matches_authoritative_baseline() -> None:
    """当前派生文件必须与 §31.2 权威表逐字一致，非权威表登记完备。"""
    assert cc.check_phase_map_against_baseline() is True


def test_phase_map_detects_authoritative_text_drift(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Phase 2 曾被改成「允许 CreatorRelease」，与基线相反却全绿——必须报红。"""
    import yaml

    path = _phase_fixture(monkeypatch, tmp_path)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    phase2 = next(p for p in data["phases"] if p["id"] == "Phase 2")
    phase2["community"] = "组织内社区 MVP；Phase 2 允许 Creator Release 组织内发布。"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")

    assert cc.check_phase_map_against_baseline() is False


def test_phase_map_detects_missing_non_authoritative_registration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """基线里真实存在的非权威阶段表未登记 → 报红（手写清单的腐烂方向是漏检）。"""
    import yaml

    path = _phase_fixture(monkeypatch, tmp_path)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert len(data["non_authoritative_phase_tables"]) == 2
    data["non_authoritative_phase_tables"].pop()
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")

    assert cc.check_phase_map_against_baseline() is False


def test_phase_map_detects_stale_registry_line(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """章节还在、行号陈旧也必须报红：登记项不再是「指向证据的指针」。"""
    import yaml

    path = _phase_fixture(monkeypatch, tmp_path)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["non_authoritative_phase_tables"][0]["line"] = 2719
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")

    assert cc.check_phase_map_against_baseline() is False


def test_phase_map_detects_phantom_registry_entry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """登记了基线中不存在的章节也要报红，否则陈旧登记可以永久滞留。"""
    import yaml

    path = _phase_fixture(monkeypatch, tmp_path)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["non_authoritative_phase_tables"].append(
        {"line": 9999, "section": "99.9 不存在的阶段表", "note": "测试"}
    )
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")

    assert cc.check_phase_map_against_baseline() is False
