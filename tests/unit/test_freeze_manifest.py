"""Phase 0 冻结包清单（freeze_manifest）测试。

这一层守的是第三轮审查最核心的指控：**「contract_check 内部自洽」不等于
「符合 V2.0.5 基线」**。因此测试必须证明：任何绕过修订流程的改动都会报红，
而不是只证明「当前仓库是绿的」。

覆盖三层判定：
1. 权威源未变（基线主文档字节数/哈希）；
2. 派生文件未私改（清单哈希 + 自指哈希）；
3. 演进有据（实际 == 基线 ∪ 已批准修订，双向精确）。

每条负向用例都对应一种真实可能发生的绕过手法。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import freeze_manifest as fm  # noqa: E402


# --------------------------------------------------------------------------- #
# 测试夹具：一套最小但完整的临时冻结包
# --------------------------------------------------------------------------- #
def _write_yaml(path: Path, data: object) -> Path:
    import yaml

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


def _write_schema(path: Path, names: list[str]) -> Path:
    import json

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"$defs": {n: {"type": "object"} for n in names}}, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def _registry(keys: list[str]) -> dict[str, object]:
    return {
        "total_actions": len(keys),
        "actions": [{"action_key": k} for k in keys],
    }


def _state_machines(names: list[str]) -> dict[str, object]:
    return {"objects": [{"object": n} for n in names]}


def _openapi(keys: list[str]) -> dict[str, object]:
    return {"paths": {f"/{k.lower()}": {"get": {"operationId": k}} for k in keys}}


def _amendment(
    amendment_id: str = "T-A1",
    *,
    added_actions: list[str] | None = None,
    added_objects: list[str] | None = None,
    added_machines: list[str] | None = None,
    removed_actions: list[str] | None = None,
) -> dict[str, object]:
    return {
        "id": amendment_id,
        "title": "测试修订",
        "reason": "测试用",
        "adr": "docs/adr/x.md",
        "approved_by": "test",
        "approved_at": "2026-11-16",
        "additions": {
            "actions": [{"action_key": k, "why": "t"} for k in (added_actions or [])],
            "schema_objects": [{"object": o, "why": "t"} for o in (added_objects or [])],
            "state_machines": [{"object": m, "why": "t"} for m in (added_machines or [])],
        },
        "removals": {"actions": [{"action_key": k} for k in (removed_actions or [])]},
    }


class FrozenPack:
    """临时冻结包：基线 3 Action / 2 对象 / 2 状态机 + 1 条修订。"""

    BASE_ACTIONS = ["A.ONE", "A.TWO", "A.THREE"]
    BASE_OBJECTS = ["ObjOne", "ObjTwo"]
    BASE_MACHINES = ["ObjOne", "ObjTwo"]
    ADDED_ACTIONS = ["A.READ"]
    ADDED_OBJECTS = ["ObjThree"]
    ADDED_MACHINES = ["ObjThree"]

    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.root = root
        self.doc = root / "docs" / "baseline" / "V2.0.5.md"
        self.doc.parent.mkdir(parents=True, exist_ok=True)
        self.doc.write_text("# 基线正文\n内容\n", encoding="utf-8")

        self.inventory = root / "contracts" / "phase0" / "frozen_inventory.yaml"
        self.amendments = root / "contracts" / "phase0" / "contract_amendments.yaml"
        self.manifest = root / "contracts" / "phase0" / "phase0_manifest.md"
        self.manifest_sha = root / "contracts" / "phase0" / "phase0_manifest.md.sha256"
        self.registry = root / "contracts" / "action_registry" / "action_registry.yaml"
        self.openapi_path = root / "contracts" / "openapi" / "openapi.draft.yaml"
        self.schema = root / "contracts" / "schemas" / "object_schemas.schema.json"
        self.machines = root / "contracts" / "state_machines" / "state_machines.yaml"

        self._monkeypatch = monkeypatch
        for name, value in (
            ("ROOT", root),
            ("BASELINE_DOC", self.doc),
            ("INVENTORY_PATH", self.inventory),
            ("AMENDMENTS_PATH", self.amendments),
            ("MANIFEST_PATH", self.manifest),
            ("MANIFEST_SHA_PATH", self.manifest_sha),
            ("ACTION_REGISTRY", self.registry),
            ("OPENAPI", self.openapi_path),
            ("OBJECT_SCHEMAS", self.schema),
            ("STATE_MACHINES", self.machines),
        ):
            monkeypatch.setattr(fm, name, value)
        # 十一类产物在测试里收敛成 3 个真实文件（自指项 + 两个普通项）。
        monkeypatch.setattr(
            fm,
            "ARTIFACTS",
            (
                fm.Artifact("① 对象 Schema", self.schema, "8.x"),
                fm.Artifact("④ Action Registry", self.registry, "31.6"),
                fm.Artifact("⑧ 冻结包清单", self.manifest, "31.11", self_reference=True),
            ),
        )
        self.write_valid()

    def _baseline_block(self) -> dict[str, object]:
        import hashlib

        return {
            "document": fm._rel(self.doc),
            "document_bytes": self.doc.stat().st_size,
            "document_sha256": hashlib.sha256(self.doc.read_bytes()).hexdigest(),
            "revision": "TEST-R1",
            "frozen_at_source_commit": "deadbee",
            "counts": {
                "actions": len(self.BASE_ACTIONS),
                "openapi_operations": len(self.BASE_ACTIONS),
                "schema_objects": len(self.BASE_OBJECTS),
                "state_machines": len(self.BASE_MACHINES),
                "p0_freeze_objects": len(self.BASE_OBJECTS),
                "display_annex_objects": 0,
            },
        }

    def write_inventory(self) -> None:
        _write_yaml(
            self.inventory,
            {
                "baseline": self._baseline_block(),
                "actions": self.BASE_ACTIONS,
                "schema_objects": self.BASE_OBJECTS,
                "state_machines": self.BASE_MACHINES,
            },
        )

    def write_valid(self) -> None:
        """写出一套「基线 + 一条已批准修订」的合规冻结包。"""
        self.write_inventory()
        _write_yaml(
            self.amendments,
            {
                "amendments": [
                    _amendment(
                        added_actions=self.ADDED_ACTIONS,
                        added_objects=self.ADDED_OBJECTS,
                        added_machines=self.ADDED_MACHINES,
                    )
                ],
                "revocations": [],
            },
        )
        self.write_contracts(
            actions=[*self.BASE_ACTIONS, *self.ADDED_ACTIONS],
            objects=[*self.BASE_OBJECTS, *self.ADDED_OBJECTS],
            machines=[*self.BASE_MACHINES, *self.ADDED_MACHINES],
        )
        fm.write_manifest()

    def write_contracts(
        self,
        *,
        actions: list[str],
        objects: list[str],
        machines: list[str],
    ) -> None:
        _write_yaml(self.registry, _registry(actions))
        _write_yaml(self.openapi_path, _openapi(actions))
        _write_schema(self.schema, objects)
        _write_yaml(self.machines, _state_machines(machines))


@pytest.fixture
def pack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FrozenPack:
    return FrozenPack(tmp_path, monkeypatch)


# --------------------------------------------------------------------------- #
# 正向：合规包必须通过
# --------------------------------------------------------------------------- #
def test_valid_pack_passes(pack: FrozenPack) -> None:
    assert fm.check_all() == []


def test_valid_pack_cli_check_exits_zero(pack: FrozenPack) -> None:
    assert fm.main(["--check"]) == 0


def test_write_then_check_is_idempotent(pack: FrozenPack) -> None:
    """重复 --write 不改变结论（生成器确定性，不靠时间戳/随机序）。"""
    first = pack.manifest.read_text(encoding="utf-8")
    fm.write_manifest()
    assert pack.manifest.read_text(encoding="utf-8") == first
    assert fm.check_all() == []


# --------------------------------------------------------------------------- #
# 第一层：权威源被改动
# --------------------------------------------------------------------------- #
def test_baseline_document_hash_drift_detected(pack: FrozenPack) -> None:
    """正文被改（哪怕一个字符）→ 必须报「新修订」，不许沿用旧清单。"""
    pack.doc.write_text("# 基线正文\n内容改动\n", encoding="utf-8")
    problems = fm.check_all()
    assert any("基线主文档 SHA-256 变更" in p for p in problems)


def test_missing_baseline_document_detected(pack: FrozenPack) -> None:
    pack.doc.unlink()
    problems = fm.check_all()
    assert any("缺少基线主文档" in p for p in problems)


# --------------------------------------------------------------------------- #
# 第二层：派生产物被私改
# --------------------------------------------------------------------------- #
def test_artifact_content_change_detected(pack: FrozenPack) -> None:
    """契约文件被改但清单没重签 → 报「清单未重签」，不能靠重生成绕过。"""
    pack.registry.write_text(
        pack.registry.read_text(encoding="utf-8") + "\n# 手改\n", encoding="utf-8"
    )
    problems = fm.check_all()
    assert any("内容已变更但清单未重签" in p for p in problems)


def test_manifest_tampered_detected(pack: FrozenPack) -> None:
    """手改清单正文（把哈希换成新的）→ 自指哈希校验必须拦住。"""
    text = pack.manifest.read_text(encoding="utf-8").replace("基线正文", "基线正文")
    pack.manifest.write_text(text + "\n<!-- 手改 -->\n", encoding="utf-8")
    problems = fm.check_all()
    assert any("清单自身哈希与 .sha256 不符" in p for p in problems)


def test_missing_manifest_detected(pack: FrozenPack) -> None:
    pack.manifest.unlink()
    problems = fm.check_all()
    assert any("缺少冻结包清单" in p for p in problems)


# --------------------------------------------------------------------------- #
# 第三层：演进有据（这是本闸门的核心价值）
# --------------------------------------------------------------------------- #
def test_unregistered_action_detected(pack: FrozenPack) -> None:
    """直接往注册表塞一个 Action（未经修订登记）→ 必须报未登记新增。

    这正是历史上真实发生的形态：为让读接口能上线，直接改派生文件。
    """
    pack.write_contracts(
        actions=[*pack.BASE_ACTIONS, *pack.ADDED_ACTIONS, "A.SNEAK"],
        objects=[*pack.BASE_OBJECTS, *pack.ADDED_OBJECTS],
        machines=[*pack.BASE_MACHINES, *pack.ADDED_MACHINES],
    )
    problems = fm.check_amendment_consistency(fm._load_yaml(pack.inventory))
    assert any("未登记新增" in p and "A.SNEAK" in p for p in problems)


def test_unregistered_schema_object_detected(pack: FrozenPack) -> None:
    pack.write_contracts(
        actions=[*pack.BASE_ACTIONS, *pack.ADDED_ACTIONS],
        objects=[*pack.BASE_OBJECTS, *pack.ADDED_OBJECTS, "Sneaky"],
        machines=[*pack.BASE_MACHINES, *pack.ADDED_MACHINES],
    )
    problems = fm.check_amendment_consistency(fm._load_yaml(pack.inventory))
    assert any("Schema 对象" in p and "Sneaky" in p for p in problems)


def test_missing_baseline_action_detected(pack: FrozenPack) -> None:
    """删掉一个基线 Action 凑数量（「删 6 加 6」式伪装）→ 必须报缺失。"""
    pack.write_contracts(
        actions=["A.TWO", "A.THREE", *pack.ADDED_ACTIONS],
        objects=[*pack.BASE_OBJECTS, *pack.ADDED_OBJECTS],
        machines=[*pack.BASE_MACHINES, *pack.ADDED_MACHINES],
    )
    problems = fm.check_amendment_consistency(fm._load_yaml(pack.inventory))
    assert any("缺少基线/登记项" in p and "A.ONE" in p for p in problems)


def test_stale_amendment_detected(pack: FrozenPack) -> None:
    """修订登记了但实际没实现（或已删）→ 报陈旧登记，台账不许失真。"""
    pack.write_contracts(
        actions=list(pack.BASE_ACTIONS),
        objects=[*pack.BASE_OBJECTS, *pack.ADDED_OBJECTS],
        machines=[*pack.BASE_MACHINES, *pack.ADDED_MACHINES],
    )
    problems = fm.check_amendment_consistency(fm._load_yaml(pack.inventory))
    assert any("修订登记陈旧" in p for p in problems)


def test_duplicate_registration_of_baseline_item_detected(pack: FrozenPack) -> None:
    """把基线已有的条目再登记成「增补」→ 重复登记，属台账噪声，必须报红。"""
    _write_yaml(
        pack.amendments,
        {"amendments": [_amendment(added_actions=["A.ONE"])], "revocations": []},
    )
    problems = fm.check_amendment_consistency(fm._load_yaml(pack.inventory))
    assert any("重复登记" in p for p in problems)


def test_inventory_count_mismatch_detected(pack: FrozenPack) -> None:
    """改了名称列表忘了改 counts（或反之）→ 自述数量与全集不一致必须报红。"""
    data = fm._load_yaml(pack.inventory)
    data["baseline"]["counts"]["actions"] = 99
    _write_yaml(pack.inventory, data)
    problems = fm.check_amendment_consistency(data)
    assert any("不一致" in p for p in problems)


def test_openapi_parity_detected(pack: FrozenPack) -> None:
    """OpenAPI 操作数与 Action 数脱钩 → 派生关系断裂。"""
    _write_yaml(pack.openapi_path, _openapi(["A.ONE"]))
    problems = fm.check_openapi_parity()
    assert any("派生关系断裂" in p for p in problems)


# --------------------------------------------------------------------------- #
# 坏形状：结构化 FAIL，而不是 traceback
# --------------------------------------------------------------------------- #
def test_amendment_missing_field_raises_readable_error(pack: FrozenPack) -> None:
    """修订缺 approved_by（说不清谁批的）→ 结构化失败，不能静默通过。"""
    bad = _amendment()
    bad.pop("approved_by")
    _write_yaml(pack.amendments, {"amendments": [bad], "revocations": []})
    with pytest.raises(fm.FreezeDriftError) as exc_info:
        fm.load_amendments()
    assert "approved_by" in str(exc_info.value)


def test_amendment_bad_shape_raises_readable_error(pack: FrozenPack) -> None:
    """修订条目写成字符串 → 报「必须是映射」，而不是 AttributeError。"""
    _write_yaml(pack.amendments, {"amendments": ["oops"], "revocations": []})
    with pytest.raises(fm.FreezeDriftError) as exc_info:
        fm.load_amendments()
    assert "必须是映射" in str(exc_info.value)


def test_duplicate_amendment_id_detected(pack: FrozenPack) -> None:
    _write_yaml(
        pack.amendments,
        {"amendments": [_amendment(), _amendment()], "revocations": []},
    )
    with pytest.raises(fm.FreezeDriftError) as exc_info:
        fm.load_amendments()
    assert "id 重复" in str(exc_info.value)


def test_duplicate_action_key_in_registry_detected(pack: FrozenPack) -> None:
    _write_yaml(pack.registry, _registry(["A.ONE", "A.ONE"]))
    with pytest.raises(fm.FreezeDriftError) as exc_info:
        fm.current_actions()
    assert "重复 action_key" in str(exc_info.value)


def test_total_actions_field_mismatch_detected(pack: FrozenPack) -> None:
    """``total_actions`` 与条目数不一致（手写的数量副本）→ 必须报红。"""
    _write_yaml(pack.registry, {"total_actions": 99, "actions": [{"action_key": "A.ONE"}]})
    with pytest.raises(fm.FreezeDriftError) as exc_info:
        fm.current_actions()
    assert "total_actions" in str(exc_info.value)


def test_check_cli_reports_failures_without_traceback(pack: FrozenPack) -> None:
    """CLI 遇到漂移应打印 [FAIL] 并返回 1，而不是抛栈。"""
    pack.doc.write_text("# 被改\n", encoding="utf-8")
    assert fm.main(["--check"]) == 1
