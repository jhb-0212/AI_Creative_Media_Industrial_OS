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
    source_commits: list[str] | None = None,
    approved_attachments: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    entry: dict[str, object] = {
        "id": amendment_id,
        "title": "测试修订",
        "reason": "测试用",
        "adr": "docs/adr/x.md",
        "approved_by": "test",
        "approved_at": "2026-11-16",
        # 缺省给一个非空提交：可追溯性是必填项，测试夹具不应默认豁免它。
        "source_commits": ["abc1234"] if source_commits is None else source_commits,
        "additions": {
            "actions": [{"action_key": k, "why": "t"} for k in (added_actions or [])],
            "schema_objects": [{"object": o, "why": "t"} for o in (added_objects or [])],
            "state_machines": [{"object": m, "why": "t"} for m in (added_machines or [])],
        },
        "removals": {"actions": [{"action_key": k} for k in (removed_actions or [])]},
    }
    # 只有用到附件的用例才写这个键：附件的要求侧对绝大多数修订不适用，
    # 夹具默认带上它会让「没有该键 = 与附件无关」这条语义失去测试覆盖。
    if approved_attachments is not None:
        entry["frozen_attachments"] = approved_attachments
    return entry


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
        self.attachment = root / "docs" / "baseline" / "RESOLUTION.md"
        self.attachments: list[dict[str, object]] = []

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
        data: dict[str, object] = {
            "baseline": self._baseline_block(),
            "actions": self.BASE_ACTIONS,
            "schema_objects": self.BASE_OBJECTS,
            "state_machines": self.BASE_MACHINES,
        }
        # 只有用到附件的用例才写这个键：其余用例必须继续走「缺省无附件」的路径，
        # 否则「没有 attachments 段就崩」这种回归会被夹具自己盖住。
        if self.attachments:
            data["attachments"] = self.attachments
        _write_yaml(self.inventory, data)

    def add_attachment(self, *, content: str = "# 冻结决议\n测试正文\n") -> Path:
        """登记一份冻结附件并同步机读清单（不重签冻结包清单）。"""
        import hashlib

        self.attachment.parent.mkdir(parents=True, exist_ok=True)
        self.attachment.write_text(content, encoding="utf-8")
        self.attachments = [
            {
                "path": fm._rel(self.attachment),
                "bytes": self.attachment.stat().st_size,
                "sha256": hashlib.sha256(self.attachment.read_bytes()).hexdigest(),
                "role": "冻结决议（测试）",
            }
        ]
        self.write_inventory()
        return self.attachment

    def authorize_attachment(self) -> None:
        """让修订台账批准当前登记的那份附件，并重签清单（要求侧落纸）。"""
        approved = [
            {"path": entry["path"], "bytes": entry["bytes"], "sha256": entry["sha256"]}
            for entry in self.attachments
        ]
        _write_yaml(
            self.amendments,
            {
                "amendments": [
                    _amendment(
                        added_actions=self.ADDED_ACTIONS,
                        added_objects=self.ADDED_OBJECTS,
                        added_machines=self.ADDED_MACHINES,
                        approved_attachments=approved,
                    )
                ],
                "revocations": [],
            },
        )
        fm.write_manifest()

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
# 第二层补：冻结附件（治理文件）
#
# 这一层守的是「原件缺失」这条阻塞项：冻结的依据如果只存在于某份审计的正文里，
# 它随时可以被悄悄替换而所有闸门仍绿。附件登记后，字节与哈希必须与清单同时成立。
# --------------------------------------------------------------------------- #
def test_registered_attachment_passes(pack: FrozenPack) -> None:
    pack.add_attachment()
    fm.write_manifest()
    assert fm.check_all() == []


def test_attachment_content_change_detected(pack: FrozenPack) -> None:
    """决议正文被改 → 报「附件内容已变更」，且不提示重签清单（那是掩盖）。"""
    attachment = pack.add_attachment()
    fm.write_manifest()
    attachment.write_text("# 冻结决议\n改过的正文\n", encoding="utf-8")
    problems = fm.check_all()
    assert any("附件内容已变更" in p for p in problems)
    assert any("须重新裁决" in p for p in problems)


def test_missing_attachment_detected(pack: FrozenPack) -> None:
    """只有哈希没有文件，正是「引用 20 处、原件 0 份」的复发形态。"""
    attachment = pack.add_attachment()
    fm.write_manifest()
    attachment.unlink()
    problems = fm.check_all()
    assert any("附件缺失" in p for p in problems)


def test_attachment_not_registered_in_manifest_detected(pack: FrozenPack) -> None:
    """登记进机读清单但没重签冻结包清单 → 清单看不见它，等于没登记。"""
    pack.add_attachment()
    problems = fm.check_all()
    assert any("清单未登记附件" in p for p in problems)


def test_attachment_missing_field_raises_readable_error(pack: FrozenPack) -> None:
    import hashlib

    attachment = pack.add_attachment()
    pack.attachments = [
        {
            "path": fm._rel(attachment),
            "bytes": attachment.stat().st_size,
            "sha256": hashlib.sha256(attachment.read_bytes()).hexdigest(),
        }
    ]
    pack.write_inventory()
    with pytest.raises(fm.FreezeDriftError, match="role"):
        fm.check_all()


# --------------------------------------------------------------------------- #
# 第二层补（要求侧）：附件登记不得被静默移除
#
# 上面的用例证明「登记了什么就验什么」。但要求若只写在登记里，删掉整段
# attachments 就让要求与登记一起消失——闸门会把「本来就没有附件」与「附件被
# 悄悄移除」判成同一件事。所以要求侧必须另立在修订台账，并由下面这组用例证明
# 它真的会拦人。
# --------------------------------------------------------------------------- #
def test_approved_attachment_passes(pack: FrozenPack) -> None:
    pack.add_attachment()
    pack.authorize_attachment()
    assert fm.check_all() == []


def test_removing_attachment_registration_detected(pack: FrozenPack) -> None:
    """删掉整段 attachments 登记 → 必须报「批准纳入冻结面的附件未登记」。

    这是本层的核心用例：登记侧的用例全绿，正因为它们验的是「登记了什么」，
    而这里登记已经不存在了——唯一还能说话的是修订台账里那笔批准。
    （清单此时仍是旧的、还登着那份附件；--write 会先一步拒绝，见下一条用例。）
    """
    pack.add_attachment()
    pack.authorize_attachment()
    pack.attachments = []
    pack.write_inventory()

    problems = fm.check_all()
    assert any("批准纳入冻结面的附件未登记" in p for p in problems), problems
    assert any("T-A1" in p for p in problems), problems


def test_attachment_swapped_with_registration_detected(pack: FrozenPack) -> None:
    """换文件 + 改登记（登记侧已自洽）→ 批准时的指纹仍会拦下。

    只锁路径的话，「换掉冻结依据」只需「换文件 + 改登记 + 重签」三步就能全绿；
    把批准时的字节与哈希记在修订台账里，换内容就必须连已批准修订一起改——
    那是一次必须被看见的 governance diff。
    """
    attachment = pack.add_attachment()
    pack.authorize_attachment()

    attachment.write_text("# 换过的决议\n", encoding="utf-8")
    import hashlib

    pack.attachments = [
        {
            "path": fm._rel(attachment),
            "bytes": attachment.stat().st_size,
            "sha256": hashlib.sha256(attachment.read_bytes()).hexdigest(),
            "role": "冻结决议（测试）",
        }
    ]
    pack.write_inventory()

    # 不加任何断言也能看出：这条消息只可能来自要求侧（登记侧此时是自洽的）。
    problems = fm.check_all()
    assert any("批准的内容不一致" in p for p in problems), problems
    with pytest.raises(fm.FreezeDriftError, match="拒绝生成清单"):
        fm.write_manifest()


def test_write_refuses_when_approved_attachment_missing(pack: FrozenPack) -> None:
    """要求侧不成立时 --write 必须拒绝：不签一份「附件表里没有它」的清单。

    与 check 报红不是重复：--write 是唯一能产出清单的入口，若它照写，攻击者只需
    「删登记 + 重签」就能得到一份自洽的清单，只等某次 check 被跳过。
    """
    pack.add_attachment()
    pack.authorize_attachment()
    pack.attachments = []
    pack.write_inventory()
    with pytest.raises(fm.FreezeDriftError, match="拒绝生成清单"):
        fm.write_manifest()


def test_conflicting_approvals_detected(pack: FrozenPack) -> None:
    """同一份附件被两条修订批准成不同版本 → 要求侧自相矛盾，必须报红。"""
    attachment = pack.add_attachment()
    spec = {
        "path": fm._rel(attachment),
        "bytes": attachment.stat().st_size,
        "sha256": "0" * 64,
    }
    _write_yaml(
        pack.amendments,
        {
            "amendments": [
                _amendment("T-A1", approved_attachments=[spec]),
                _amendment("T-A2", approved_attachments=[{**spec, "sha256": "1" * 64}]),
            ],
            "revocations": [],
        },
    )
    problems = fm.check_attachment_authorization(
        fm._load_yaml(pack.inventory), fm.load_amendments()
    )
    assert any("批准为不同内容" in p for p in problems), problems


def test_absolute_attachment_path_rejected(pack: FrozenPack) -> None:
    """绝对路径 → 拒绝：校验读的与仓库里的必须是同一个文件。"""
    attachment = pack.add_attachment()
    pack.attachments[0]["path"] = str(attachment)
    pack.write_inventory()
    with pytest.raises(fm.FreezeDriftError, match="相对路径"):
        fm.check_all()


def test_parent_escape_attachment_path_rejected(pack: FrozenPack) -> None:
    """`..` 上跳到仓库外 → 拒绝（冻结依据不能指向版本控制之外的文件）。"""
    pack.add_attachment()
    pack.attachments[0]["path"] = "../../Phase0-冻结评审决议.md"
    pack.write_inventory()
    with pytest.raises(fm.FreezeDriftError, match="上跳"):
        fm.check_all()


def test_symlink_attachment_rejected(pack: FrozenPack) -> None:
    """符号链接 → 拒绝：字节必须直接来自仓库内的这个文件，而不是一层间接引用。"""
    attachment = pack.add_attachment()
    link = attachment.parent / "LINK.md"
    try:
        link.symlink_to(attachment)
    except (OSError, NotImplementedError):
        pytest.skip("当前环境不允许创建符号链接（Windows 需开发者模式/管理员）")
    pack.attachments[0]["path"] = fm._rel(link)
    pack.write_inventory()
    with pytest.raises(fm.FreezeDriftError, match="符号链接"):
        fm.check_all()


# --------------------------------------------------------------------------- #
# 仓库级：这两层判据都改掉才算真删，而那必然是一次显式 diff
# --------------------------------------------------------------------------- #
# Phase 0 冻结决议必须一直留在附件登记里，且由登记它入仓的那条修订批准。
# 与 verification_blocks 的白名单同理：数据驱动判据能拦住「登记没了」，
# 拦不住「连批准一起删」——后者只有在白名单里点名才看得见。
_REQUIRED_ATTACHMENTS = {
    "docs/baseline/Phase0-冻结评审决议.md": "V205-P0-FREEZE-RESOLUTION-REGISTER",
}


def test_repo_freeze_attachment_stays_registered_and_approved() -> None:
    inventory = fm._load_yaml(fm.INVENTORY_PATH)
    registered = {entry.path: entry for entry in fm.attachment_entries(inventory)}
    approved = {
        (amendment.id, att.path): att
        for amendment in fm.load_amendments()
        for att in amendment.approved_attachments
    }
    for path, amendment_id in _REQUIRED_ATTACHMENTS.items():
        assert path in registered, f"冻结附件登记被移除：{path}"
        spec = approved.get((amendment_id, path))
        assert spec is not None, f"修订 {amendment_id} 不再批准冻结附件 {path}"
        entry = registered[path]
        assert (entry.size, entry.sha256) == (spec.size, spec.sha256), (
            f"{path} 的登记内容与 {amendment_id} 批准的不一致"
        )


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


# --------------------------------------------------------------------------- #
# 可追溯性：source_commits 必填 + 历史债务双向校验
#
# 这一组守的是「规则写在注释里、判据写在代码里」的分叉：台账头部曾自称
# 「source_commits 缺一项即 FAIL」，而判据里根本没有这个字段，于是 22 条修订
# 用空列表冒充已追溯，闸门全绿。修法不是把规则从注释里删掉（那是降标准），
# 而是让判据执行规则，并把无法凭空补齐的历史挂成可枚举债务。
# --------------------------------------------------------------------------- #
def test_amendment_empty_source_commits_is_rejected(pack: FrozenPack) -> None:
    """空列表不等于已追溯：``str([]) == "[]"`` 非空，靠 str() 判空会漏。"""
    _write_yaml(
        pack.amendments,
        {
            "amendments": [_amendment(source_commits=[])],
            "revocations": [],
        },
    )
    with pytest.raises(fm.FreezeDriftError) as exc_info:
        fm.load_amendments()
    assert "source_commits" in str(exc_info.value)


def test_amendment_blank_source_commit_is_rejected(pack: FrozenPack) -> None:
    """``['  ']`` 也不能算追溯：列表里必须有非空字符串。"""
    _write_yaml(
        pack.amendments,
        {
            "amendments": [_amendment(source_commits=["  "])],
            "revocations": [],
        },
    )
    with pytest.raises(fm.FreezeDriftError) as exc_info:
        fm.load_amendments()
    assert "source_commits" in str(exc_info.value)


def test_registered_traceability_debt_is_accepted(pack: FrozenPack) -> None:
    """已在 traceability_debt 登记的历史条目允许缺 source_commits。"""
    _write_yaml(
        pack.amendments,
        {
            "amendments": [_amendment(amendment_id="OLD-A1", source_commits=[])],
            "traceability_debt": ["OLD-A1"],
            "revocations": [],
        },
    )
    amendments = fm.load_amendments()

    assert [a.id for a in amendments] == ["OLD-A1"]


def test_stale_traceability_debt_is_rejected(pack: FrozenPack) -> None:
    """登记了债务但条目其实已补齐 → 陈旧债务同样是债，必须删条目。

    不做这条反向校验，债务清单会只增不减，最终变成第二份白名单：
    真实缺口补上了也没人知道，新的缺口混在里面也看不出来。
    """
    _write_yaml(
        pack.amendments,
        {
            "amendments": [_amendment(source_commits=["abc1234"])],
            "traceability_debt": ["T-A1"],
            "revocations": [],
        },
    )
    with pytest.raises(fm.FreezeDriftError) as exc_info:
        fm.load_amendments()
    assert "traceability_debt" in str(exc_info.value)


def test_unknown_id_in_traceability_debt_is_rejected(pack: FrozenPack) -> None:
    """债务条目指向不存在的修订 id：要么是笔误，要么是删了修订没删债务。"""
    _write_yaml(
        pack.amendments,
        {
            "amendments": [_amendment()],
            "traceability_debt": ["NO-SUCH-ID"],
            "revocations": [],
        },
    )
    with pytest.raises(fm.FreezeDriftError) as exc_info:
        fm.load_amendments()
    assert "traceability_debt" in str(exc_info.value)


def test_traceability_debt_does_not_excuse_other_missing_fields(pack: FrozenPack) -> None:
    """例外必须窄：登记了债务也不能顺带豁免 reason 缺失。"""
    bad = _amendment(source_commits=[])
    bad.pop("reason")
    _write_yaml(
        pack.amendments,
        {
            "amendments": [bad],
            "traceability_debt": ["T-A1"],
            "revocations": [],
        },
    )
    with pytest.raises(fm.FreezeDriftError) as exc_info:
        fm.load_amendments()
    assert "reason" in str(exc_info.value)


def test_traceability_debt_bad_shape_raises_readable_error(pack: FrozenPack) -> None:
    """债务清单里混进非字符串 → 报结构错误，而不是把 None 当 id。"""
    _write_yaml(
        pack.amendments,
        {
            "amendments": [_amendment()],
            "traceability_debt": [123],
            "revocations": [],
        },
    )
    with pytest.raises(fm.FreezeDriftError) as exc_info:
        fm.load_amendments()
    assert "traceability_debt" in str(exc_info.value)


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


# --------------------------------------------------------------------------- #
# 第一层补充：基线正文的事实源唯一性（N-01 / ADR-R22）
# --------------------------------------------------------------------------- #
def test_stray_baseline_copy_detected(pack: FrozenPack) -> None:
    """仓库根出现一份同名正文副本 → 必须报「游离副本」。

    这是历史真实形态：根目录副本被手工回填过修订，README 还把它称为「配套基线文档」，
    于是同一件事出现 75 / 83 / 84 三个数字，而固定路径校验全程绿灯。
    """
    stray = pack.root / pack.doc.name
    stray.write_text("# 另一份正文\n", encoding="utf-8")
    problems = fm.check_stray_baseline_copies()
    assert any("游离副本" in p and pack.doc.name in p for p in problems)


def test_stray_copy_with_identical_content_still_detected(pack: FrozenPack) -> None:
    """副本内容与权威源完全一致，也要报红。

    判定口径是「同名即歧义」：内容相同不代表没问题——下一个人改的可能是没受校验的那一份。
    """
    stray = pack.root / pack.doc.name
    stray.write_bytes(pack.doc.read_bytes())
    assert len(fm.check_stray_baseline_copies()) == 1


def test_stray_copy_with_different_case_detected(pack: FrozenPack) -> None:
    """大小写变体也是副本。

    Windows 上 `…V2.0.5.MD` 与 `…V2.0.5.md` 本就是同一个文件；而 Linux CI 大小写敏感，
    精确匹配会让它以「另一个文件名」的身份逃过闸门。判定按 casefold 比对。
    """
    stray = pack.root / pack.doc.name.upper()
    stray.write_text("# 大小写变体\n", encoding="utf-8")
    assert len(fm.check_stray_baseline_copies()) == 1


def test_stray_copy_in_dependency_dir_ignored(pack: FrozenPack) -> None:
    """依赖/缓存目录里的同名文件不算游离副本（扫描边界的正向说明）。"""
    buried = pack.root / ".venv" / "lib" / pack.doc.name
    buried.parent.mkdir(parents=True, exist_ok=True)
    buried.write_text("依赖包自带文件\n", encoding="utf-8")
    assert fm.check_stray_baseline_copies() == []


def test_authoritative_baseline_is_not_reported_as_stray(pack: FrozenPack) -> None:
    """权威路径自身不得被判为副本（否则闸门天天红，等于没有闸门）。"""
    assert fm.check_stray_baseline_copies() == []


def test_baseline_self_reported_action_count_mismatch_detected(pack: FrozenPack) -> None:
    """正文自述「当前共注册 N 项」与冻结清单的基线数不一致 → 必须报红。

    这正是副本写「83」的成因：回填 8 条修订、数了自己加的行，而真实是 75+9=84。
    """
    pack.doc.write_text(
        "# 基线正文\n当前共注册 99 项规范 action_key；\n", encoding="utf-8"
    )
    problems = fm.check_baseline_self_reported_counts(fm._load_yaml(pack.inventory))
    assert any("自述 action 数 99" in p for p in problems)


def test_baseline_self_reported_action_count_consistent(pack: FrozenPack) -> None:
    """自述数量等于冻结基线数时放行（正文是快照，不是当前注册数）。"""
    pack.doc.write_text(
        f"# 基线正文\n当前共注册 {len(pack.BASE_ACTIONS)} 项规范 action_key；\n",
        encoding="utf-8",
    )
    assert fm.check_baseline_self_reported_counts(fm._load_yaml(pack.inventory)) == []
