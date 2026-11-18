"""镜头时长与时间码的归属回归测试（ADR-R18 字段归属裁决）。

这一批用例守的是「一个事实只有一个权威源」在镜头域的具体落点。四条最容易被稀释的边界：

1. **退役要真退役**：`ShotRevision` 不再有 `duration_ms` / `timecode`，且不在 `required` 里。
   退休记录必须能指回旧定义（`git:<commit>#<pointer>`），否则「历史版本保留可读」只是句口号。
2. **承接方要真承接**：`ShotSpec.duration_ms` 曾是 `type: "string"` 的空壳。把字段从有约束的
   地方搬到无约束的地方，是把双事实源换成单一无约束事实——比原来更差。所以类型约束本身就是断言对象。
3. **不许换个名字搬回来**：`timecode` 的四元组（start / end / fps_num / fps_den）是区间事实，
   全 schema 只能有一处持有者。
4. **左闭右开是硬约定**：`[start, end)`，长度 = end − start。含末帧会让同一区间出现两个长度，
   且会在剪辑、字幕、音频、导出四处各有一个解释。

**关于 `git` 用例的诚实说明**：历史定义的可解析性靠 `git show <commit>:<path>` 验证。浅克隆里
该 commit 不可达时会 skip 并说明原因——skip 代表「本次没验证」，不代表「验证通过」。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = "contracts/schemas/object_schemas.schema.json"
SCHEMA = json.loads((ROOT / SCHEMA_PATH).read_text(encoding="utf-8"))
DEFS = SCHEMA["definitions"]

REVISION = DEFS["ShotRevision"]
SPEC = DEFS["ShotSpec"]
TIMELINE = DEFS["TimelineDraft"]

# 区间型时间码的合法持有者：只有 TimelineDraft 的镜头条目（clip_in / clip_out + fps）。
# 批注类对象（QC 问题、客户批注、审阅批注、反向关键帧引用）只有单点 timecode 定位，
# 不构成区间事实，因此不在下面的区间判定里。
INTERVAL_FIELDS = ("start_frame", "end_frame")

# 全 schema 允许出现 duration 语义字段的白名单：一个权威 + 两处落地 + 一处草案估值。
DURATION_OWNERS = {
    ("ShotSpec", "duration_ms"),  # 唯一权威（8.12 单一数据源）
    ("TimelineDraft", "duration_frames"),  # 该序列总帧数（落地结果）
    ("TimelineDraft", "duration_per_shot_frames{}"),  # 每镜头占用帧数（落地结果）
    ("ShotBreakdownDraft", "estimated_duration_ms"),  # 拆镜阶段的估值，非锁定事实
}


# --------------------------------------------------------------------------- #
# 1. 退役：ShotRevision 不再持有
# --------------------------------------------------------------------------- #
def test_shot_revision_retired_duration_and_timecode() -> None:
    assert "duration_ms" not in REVISION["properties"], "时长已归 ShotSpec，留在修订就是双事实源"
    assert "timecode" not in REVISION["properties"], "时间码已归 TimelineDraft，留在修订会让剪辑污染版本链"
    assert "duration_ms" not in REVISION["required"]
    assert "timecode" not in REVISION["required"]


def test_retired_ledger_is_machine_readable() -> None:
    ledger = {item["field"]: item for item in REVISION["x-afs-retired-fields"]}
    assert set(ledger) == {"duration_ms", "timecode"}
    for field, item in ledger.items():
        assert item["authoritative_owner"].strip(), f"{field} 没有指明去向"
        assert item["why"].strip(), f"{field} 没有给出退役理由"
        assert item["migration"].strip(), f"{field} 没有迁移口径"
        assert isinstance(item["retired_at_schema_version"], int)
        assert item["retired_at_schema_version"] >= 2, "退役版本必须是删除后的那个版本"
        assert item["historical_ref"].startswith("git:"), f"{field} 的历史引用必须是 git 定位"


def _resolve_pointer(document: object, pointer: str) -> object:
    current = document
    for part in pointer.strip("/").split("/"):
        assert isinstance(current, dict), f"指针 {pointer} 在 {part} 处不是对象"
        assert part in current, f"指针 {pointer} 在 {part} 处断裂"
        current = current[part]
    return current


def test_retired_historical_ref_resolves_to_the_old_definition() -> None:
    """退役账里的 git 引用必须真能取回旧定义，否则「历史可读」无法执行。"""
    for item in REVISION["x-afs-retired-fields"]:
        ref, _, pointer = item["historical_ref"].partition("#")
        commit = ref.split(":", 1)[1]
        completed = subprocess.run(
            ["git", "show", f"{commit}:{SCHEMA_PATH}"],
            cwd=ROOT,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            pytest.skip(
                f"commit {commit} 不可达（浅克隆？），本次未验证 {item['field']} 的历史定义"
            )
        old = json.loads(completed.stdout.decode("utf-8"))
        recovered = _resolve_pointer(old, pointer)
        assert isinstance(recovered, dict), f"{item['field']} 的旧定义不是 schema 片段"
        assert item["field"] in pointer, "历史引用必须指向被退役的字段本身"


def test_retired_field_did_not_move_back_under_another_name() -> None:
    """退役不是改名：承接方不得再出现 timecode 这个名字，修订也不得重新出现 duration。"""
    assert "timecode" not in SPEC["properties"]
    assert "timecode" not in TIMELINE["properties"]
    assert "duration_ms" not in REVISION["properties"]
    assert "clip_in_frame" not in REVISION["properties"]


# --------------------------------------------------------------------------- #
# 2. 唯一权威：全 schema 的 duration 归属清单
# --------------------------------------------------------------------------- #
def test_duration_has_exactly_one_authority() -> None:
    found = {
        (name, field)
        for name, spec in DEFS.items()
        if isinstance(spec, dict)
        for field in (spec.get("properties") or {})
        if "duration" in field.lower()
    }
    unexpected = found - DURATION_OWNERS
    assert not unexpected, (
        f"出现未登记时长字段 {sorted(unexpected)}：时长权威只能是 ShotSpec.duration_ms，"
        "其余只能是落地结果或草案估值，新增必须在此显式登记"
    )
    assert ("ShotSpec", "duration_ms") in found, "权威时长字段不见了"


def test_no_second_interval_fact_anywhere() -> None:
    """区间事实（start + end 成对的时间码对象）只能由 TimelineDraft 持有。"""
    holders = set()
    for name, spec in DEFS.items():
        if not isinstance(spec, dict):
            continue
        timecode = (spec.get("properties") or {}).get("timecode")
        if isinstance(timecode, dict) and set(INTERVAL_FIELDS) <= set(timecode.get("properties") or {}):
            holders.add(name)
    assert holders == set(), (
        f"{sorted(holders)} 仍持有区间型 timecode：区间事实的持有者是 TimelineDraft 的"
        " clip_in_frame / clip_out_frame，第二个持有者就是第二个事实源"
    )


# --------------------------------------------------------------------------- #
# 3. 承接方必须真有约束
# --------------------------------------------------------------------------- #
def test_shot_spec_duration_is_a_positive_integer() -> None:
    prop = SPEC["properties"]["duration_ms"]
    assert prop["type"] == "integer", "时长落到字符串上等于丢掉约束"
    assert prop.get("minimum", 0) >= 1, "零时长镜头无意义，minimum 必须排除 0"
    assert "duration_ms" in SPEC["required"]


def test_fps_is_a_rational_pair() -> None:
    prop = SPEC["properties"]["fps"]
    assert prop["type"] == "object", "有理帧率必须是结构体，不能退化成 '24/1' 字符串"
    assert set(prop["required"]) == {"num", "den"}
    for part in ("num", "den"):
        assert prop["properties"][part]["type"] == "integer"
        assert prop["properties"][part]["minimum"] >= 1
    assert prop["additionalProperties"] is False, "多余键会让帧率出现未定义语义"


def test_cut_points_are_strictly_ordered_by_contract() -> None:
    prop = SPEC["properties"]["cut_points_frames"]
    assert prop["type"] == "array"
    assert prop["items"]["type"] == "integer"
    assert prop["minItems"] >= 1, "至少要有一个切点（镜头起点）"
    assert prop["uniqueItems"] is True, "重复切点等于零长片段"


def test_shot_spec_holds_no_timeline_position() -> None:
    for field in ("start_frame", "end_frame", "clip_in_frame", "clip_out_frame", "shot_order"):
        assert field not in SPEC["properties"], f"{field} 属时间线位置，放进规格会让剪辑污染规格"
    excluded = json.dumps(SPEC["x-afs-exclusions"], ensure_ascii=False)
    assert "clip_in_frame" in excluded, "排除项要么删字段，要么写清理由，不能只是不说"


def test_timeline_frame_fields_are_integers() -> None:
    for field in (
        "clip_in_frame",
        "clip_out_frame",
        "start_frame",
        "end_frame",
        "fps_num",
        "fps_den",
        "duration_frames",
    ):
        prop = TIMELINE["properties"][field]
        assert prop["type"] == "integer", f"{field} 必须是整数帧，字符串时间码不可作主键"
        assert prop.get("minimum", -1) >= 0
    for field in ("fps_num", "fps_den"):
        assert TIMELINE["properties"][field]["minimum"] >= 1


def test_duration_per_shot_frames_is_a_frame_map() -> None:
    prop = TIMELINE["properties"]["duration_per_shot_frames{}"]
    assert prop["type"] == "object"
    assert prop["additionalProperties"]["type"] == "integer", "值必须是整数帧，不是字符串"
    assert prop["additionalProperties"]["minimum"] >= 0


# --------------------------------------------------------------------------- #
# 4. 左闭右开
# --------------------------------------------------------------------------- #
def test_interval_is_left_closed_right_open() -> None:
    for field in ("clip_out_frame", "end_frame"):
        text = TIMELINE["properties"][field]["description"]
        assert "右开" in text or "不含末帧" in text, f"{field} 必须写清 end 不含末帧"
    for field in ("clip_in_frame", "start_frame"):
        text = TIMELINE["properties"][field]["description"]
        assert "闭" in text, f"{field} 必须写清起点属于区间"

    ids = {item["id"] for item in TIMELINE["x-afs-invariants"]}
    assert "TL-1" in ids, "左闭右开必须是不变量，不能只写在描述里"


def test_left_closed_right_open_arithmetic() -> None:
    """8.3 的 240 → 366 @ 30/1 必须是 126 帧 / 4200 ms；含末帧会得到 127 帧。"""
    start, end = 240, 366
    frames = end - start
    assert frames == 126
    assert frames * 1000 // 30 == 4200
    inclusive_frames = end - start + 1
    assert inclusive_frames == 127
    assert inclusive_frames * 1000 // 30 == 4233, "含末帧会得到 4233 ms，与 8.3 的 4200 对不上"


def test_duration_conversion_must_not_be_strict_equality() -> None:
    """8.12 自身示例就不严格，实现方不得据此写「严格相等」校验。"""
    duration_ms, fps_num, fps_den = 4200, 24, 1
    exact_frames = duration_ms * fps_num / (fps_den * 1000)
    assert abs(exact_frames - 100.8) < 1e-9
    assert exact_frames != 100, "示例的切点末点是 100，说明换算本身带取整"
    assert not float(exact_frames).is_integer()


# --------------------------------------------------------------------------- #
# 5. 承接映射与来源
# --------------------------------------------------------------------------- #
def test_timecode_intake_mapping_targets_real_fields() -> None:
    mapping = TIMELINE["x-afs-timecode-intake"]["mapping"]
    assert set(mapping) == {"start_frame", "end_frame", "fps_num", "fps_den"}
    for source, target in mapping.items():
        assert target in TIMELINE["properties"], f"{source} 的落点 {target} 不存在"
    assert mapping["start_frame"] == "clip_in_frame"
    assert mapping["end_frame"] == "clip_out_frame"
    assert "左闭右开" in TIMELINE["x-afs-timecode-intake"]["interval"]


def test_invariant_ids_are_unique_and_scoped() -> None:
    for name, expected in (("ShotSpec", "SS-"), ("TimelineDraft", "TL-")):
        items = DEFS[name]["x-afs-invariants"]
        ids = [item["id"] for item in items]
        assert len(ids) == len(set(ids)), f"{name} 不变量 id 重复"
        assert all(item_id.startswith(expected) for item_id in ids), f"{name} 的 id 前缀不统一"
        for item in items:
            assert item["statement"].strip()
            assert item["enforced_by"] in {"SCHEMA", "RUNTIME_PENDING"}

    spec_schema = {i["id"] for i in SPEC["x-afs-invariants"] if i["enforced_by"] == "SCHEMA"}
    assert spec_schema == {"SS-1", "SS-2", "SS-4"}, (
        "ShotSpec 能由 schema 表达的只有：时长单一权威、绑定精确修订、不含时间线定位；"
        f"实际 {sorted(spec_schema)}"
    )
    timeline_schema = {i["id"] for i in TIMELINE["x-afs-invariants"] if i["enforced_by"] == "SCHEMA"}
    assert timeline_schema == {"TL-4"}, (
        "TimelineDraft 只有「引用精确修订」能由 schema 表达，区间关系都要运行时；"
        f"实际 {sorted(timeline_schema)}"
    )


def test_sources_no_longer_treat_8_3_as_authority() -> None:
    sources = " ".join(REVISION["x-afs-sources"])
    assert "8.12" in sources, "时长归属必须引 8.12 的单一数据源表述"
    assert "33.2" in sources, "时间线位置必须引 33.2 的入出点字段组"
    assert "duration_ms / timecode）" not in sources, "不得再把 8.3 的混合示例当字段清单采信"


# --------------------------------------------------------------------------- #
# 6. 决策被登记
# --------------------------------------------------------------------------- #
def test_amendment_a7_records_the_decision() -> None:
    amendments = yaml.safe_load(
        (ROOT / "contracts/phase0/contract_amendments.yaml").read_text(encoding="utf-8")
    )["amendments"]
    entry = next((item for item in amendments if item["id"] == "V205-R14-A7"), None)
    assert entry is not None, "字段归属裁决必须留修订登记，不能只改 schema"
    assert entry["adr"] == "docs/adr/ADR-R18-shot-entity-contract.md"

    risks = " ".join(entry["residual_risks"])
    assert "基线" in risks, "8.3 示例待修订必须登记"
    assert "容差" in risks or "取整" in risks, "跨对象换算缺口径必须登记"
    assert "Scene" in risks, "Scene 结构性缺口必须继续挂着"
