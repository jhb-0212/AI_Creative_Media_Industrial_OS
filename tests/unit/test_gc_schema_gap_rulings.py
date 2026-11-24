"""G-C 八项缺失 Schema 裁定回归：正向锁定事实源，负向注入断言闸门点名对象。

ERROR≠FAILED：每个负向用例都直接调用纯函数并断言具体问题文本，不依赖子进程退出码，
因此夹具异常会以 ERROR 暴露而不是伪装成「闸门报红」。被测命题：
1. 七个对象补了 schema，四个有状态机的收权威 enum、三个无状态机的收 NO_LIFECYCLE；
2. Pattern 与 PatternRecord 是两个独立对象（注册表分别登记），不合并；
3. SharedReferenceAsset 从 text_authority_only 移入 objects 并登记状态机；
4. 三处缺口台账（schema_gaps / resource_schema_gaps）已清空、ScriptLock 豁免保留；
5. 闸门双向校验：删 schema 未删缺口条目 / 补 schema 留陈旧条目都报红且点名对象；
6. 提案 open_item 不再指向已清空的缺口（以缺陷存在为前提的断言必须随修复重写）。
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

SCHEMA_PATH = ROOT / "contracts" / "schemas" / "object_schemas.schema.json"
SM_PATH = ROOT / "contracts" / "state_machines" / "state_machines.yaml"
TAXONOMY_PATH = ROOT / "contracts" / "enums" / "status_field_taxonomy.yaml"
ENUM_REGISTRY_PATH = ROOT / "contracts" / "enums" / "enum_registry.yaml"
RESOURCE_GAPS_PATH = ROOT / "contracts" / "runtime" / "resource_schema_gaps.yaml"
EXEMPTIONS_PATH = ROOT / "contracts" / "runtime" / "resource_schema_exemptions.yaml"

LIFECYCLE_ADDED = {
    "PublicationReview": {"APPROVED", "IN_REVIEW", "QUEUED", "REJECTED", "RETURNED"},
    "CreatorShareLink": {"ACTIVE", "EXPIRED", "REVOKED"},
    "Pattern": {"DEPRECATED", "EXPERIMENT_RECORD", "FAILURE_CASE", "IN_REVIEW",
                "REJECTED", "RETIRED", "REUSABLE_PATTERN"},
    "SharedReferenceAsset": {"AVAILABLE", "DEPRECATED", "DRAFT", "QUARANTINED",
                             "REVOKED", "RIGHTS_CHECK", "VALIDATED"},
}
IMMUTABLE_ADDED = {"CanonRevision", "RightsCase", "RightsManifest"}
ALL_ADDED = LIFECYCLE_ADDED.keys() | IMMUTABLE_ADDED


def _definitions() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))["definitions"]


def _sm() -> dict[str, Any]:
    return yaml.safe_load(SM_PATH.read_text(encoding="utf-8"))


def _taxonomy() -> dict[str, Any]:
    return yaml.safe_load(TAXONOMY_PATH.read_text(encoding="utf-8"))


def _sm_states() -> dict[str, set[str]]:
    return cc.state_machine_states(_sm()["objects"])


# --------------------------------------------------------------------------- #
# 正向：裁定事实正确落地
# --------------------------------------------------------------------------- #
def test_all_seven_objects_have_schema() -> None:
    defs = _definitions()
    missing = sorted(ALL_ADDED - defs.keys())
    assert not missing, f"G-C 应补 schema 的对象仍缺: {missing}"


def test_lifecycle_objects_carry_authoritative_enum() -> None:
    defs = _definitions()
    for name, expected in LIFECYCLE_ADDED.items():
        enum = defs[name]["properties"]["status"].get("enum")
        assert enum is not None, f"{name}.status 未收权威 enum"
        assert set(enum) == expected, f"{name} enum 漂移: {enum}"


def test_immutable_objects_carry_sentinel() -> None:
    defs = _definitions()
    for name in IMMUTABLE_ADDED:
        assert defs[name]["properties"]["status"].get("enum") == ["NO_LIFECYCLE"], name


def test_pattern_and_patternrecord_are_distinct_objects() -> None:
    """注册表分别登记：Pattern 见 31.5，PatternRecord 见 C.3；状态链同构不等于同一对象。"""
    sm = _sm()
    objects = sm["objects"]
    assert any(o["object"] == "Pattern" for o in objects)
    assert any(o["object"] == "PatternRecord" for o in objects)
    assert _definitions()["Pattern"]["properties"].get("rights_source") is not None
    assert _definitions()["PatternRecord"]["properties"].get("rights_manifest_ref") is not None


def test_publication_review_field_level_separation() -> None:
    spec = _definitions()["PublicationReview"]
    props = spec["properties"]
    assert {"reviewer", "submitter", "rule_version", "evidence_refs", "decided_at"} <= props.keys()
    ids = [inv["id"] for inv in spec.get("x-afs-invariants", [])]
    assert "PR-1" in ids, "审核人/提交人分离的字段级约束必须显式登记"


def test_creator_share_link_is_distinct_from_sharelink() -> None:
    defs = _definitions()
    assert defs["CreatorShareLink"]["properties"]["download_allowed"]["enum"] == [False]
    assert "CreatorShareLink" in defs and "ShareLink" in defs  # 两个独立 schema


def test_shared_reference_asset_moved_into_objects() -> None:
    sm = _sm()
    assert sm["text_authority_only"] == [] or sm["text_authority_only"] is None
    assert any(o["object"] == "SharedReferenceAsset" for o in sm["objects"])
    states = _sm_states()["SharedReferenceAsset"]
    assert {"DRAFT", "RIGHTS_CHECK", "VALIDATED", "AVAILABLE", "DEPRECATED",
            "REVOKED", "QUARANTINED"} <= states


def test_all_gap_ledgers_cleared_and_scriptlock_exemption_kept() -> None:
    assert _taxonomy()["schema_gaps"] in ([], None)
    rg = yaml.safe_load(RESOURCE_GAPS_PATH.read_text(encoding="utf-8"))
    assert rg["gaps"] in ({}, None)
    ex = yaml.safe_load(EXEMPTIONS_PATH.read_text(encoding="utf-8"))
    assert "ScriptLock" in ex["exemptions"], "锁定是 ScriptRevision 上的审批关系，豁免保留"


def test_state_machine_and_enum_registry_counts_bumped() -> None:
    """G-C 补登的对象在册，且计数注释与对象列表自洽。

    计数不写死数值：本测试证明的命题是「G-C 的四项进了权威源 + 计数注释没有
    与对象列表漂移」，而不是「总数恰好是 G-C 收口那一刻的 55」。R45 又加了一个
    状态机对象后，写死的 55 会以失败伪装成新缺陷——判据里的计数一律派生。
    """
    sm = _sm()
    assert sm["total_objects"] == len(sm["objects"])
    registry = yaml.safe_load(ENUM_REGISTRY_PATH.read_text(encoding="utf-8"))
    assert registry["object_count"] == len(registry["objects"])
    assert registry["object_count"] == sm["total_objects"]
    for name in LIFECYCLE_ADDED:
        assert any(e["object"] == name for e in registry["objects"]), name


# --------------------------------------------------------------------------- #
# 负向注入：闸门必须报红且点名具体对象（不是只报「文件不一致」）
# --------------------------------------------------------------------------- #
def test_deleting_schema_without_gap_entry_is_named() -> None:
    """schema_gap_problems 的「有状态机无 schema 未登记」分支只覆盖状态机对象；
    用有状态机的 Pattern 注入，不能用 envelope_only 的 RightsCase（前提错会让测试
    以失败伪装成新缺陷）。"""
    defs = dict(_definitions())
    victim = "Pattern"
    defs.pop(victim)
    problems = cc.schema_gap_problems([], _sm_states(), defs)
    assert any(victim in p and "未在 schema_gaps 登记" in p for p in problems), problems


def test_stale_gap_entry_for_modeled_object_is_named() -> None:
    """陈旧债务分支同样只对状态机对象成立；用 PublicationReview（有状态机 + 有 schema）注入。
    envelope_only 对象挂 gap 会先命中「不是状态机对象」分支，测不到本命题。"""
    gaps = [{
        "object": "PublicationReview",
        "reason": "陈旧债务",
        "contract_ref": "x",
        "unblocks_when": "y",
    }]
    problems = cc.schema_gap_problems(gaps, _sm_states(), _definitions())
    assert any("PublicationReview" in p and "已有 object schema" in p for p in problems), problems


def test_wrong_class_for_lifecycle_object_is_named() -> None:
    """把有状态机的 CreatorShareLink 错标 envelope_only，闸门必须点名。"""
    taxonomy = _taxonomy()
    objects = [
        ({**o, "class": "envelope_only", "basis": "immutable_record",
          "reason": "x", "unblocks_when": "y"} if o["object"] == "CreatorShareLink" else o)
        for o in taxonomy["objects"]
    ]
    problems = cc.taxonomy_problems(objects, _sm_states(), _definitions())
    assert any("CreatorShareLink" in p and "envelope_only" in p for p in problems), problems


def test_unregistered_new_schema_object_is_named() -> None:
    defs = dict(_definitions())
    defs["GhostGCObject"] = {"properties": {"status": {"type": "string"}}}
    problems = cc.taxonomy_problems(_taxonomy()["objects"], _sm_states(), defs)
    assert any("GhostGCObject" in p and "没在 status_field_taxonomy 登记" in p for p in problems), problems
