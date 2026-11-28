"""核验电池的判据测试。

这里不跑完整电池（那是分钟级且需要真实 PG），只证明它的**判据本身**不会静默失灵：

- 命令清单扩展时，未登记的闸门必须报红（第二份清单迟早漂移）；
- 类别完备性双向校验：类别缺探针、探针类别未登记，两个方向都要抓；
- 指标解析失败必须降级为「部分核验」，不能因为退出码 0 就宣称已核验；
- pytest 摘要解析要能识别 failed/error，避免「有失败却报全绿」。

负向用例对应的是「漏检表现为绿灯」这一类病——这正是规范 §5 要求派生式核验表的理由。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import merge_readiness  # noqa: E402
import verification_battery as vb  # noqa: E402


# --------------------------------------------------------------------------- #
# 命令清单派生
# --------------------------------------------------------------------------- #
def test_every_gate_command_is_registered() -> None:
    """闸门命令扩容时本用例先红：逼迫核验表为它登记类别与稳定编号。

    运行时虽有 fail-closed 兜底（未登记命令会产出 FAILED 探针），但测试更早、
    更明确——错误发生在改动那一次提交，而不是下一次有人真去读核验报告时。
    """
    commands = {name for name, _ in merge_readiness.CHECK_COMMANDS}
    registered = set(vb._COMMAND_REGISTRY)
    assert commands == registered, (
        f"未登记类别的检查器：{sorted(commands - registered)}；"
        f"登记了却已不存在的检查器：{sorted(registered - commands)}"
    )


def test_probe_ids_are_stable_and_unique() -> None:
    ids = [check_id for _, check_id in vb._COMMAND_REGISTRY.values()]
    assert len(ids) == len(set(ids))
    assert all(check_id.startswith(("C2.", "C3.", "C5.")) for check_id in ids)


# --------------------------------------------------------------------------- #
# 覆盖完备性：两个方向
# --------------------------------------------------------------------------- #
def _probe(check_id: str, category: str) -> vb.Probe:
    return vb.Probe(
        check_id=check_id,
        category=category,
        obj="测试对象",
        state=vb.VERIFIED,
        evidence=("E2",),
        location="test",
        limitation="无",
    )


def test_coverage_passes_when_every_category_has_a_probe() -> None:
    battery = vb.Battery(probes=[_probe(f"probe-{key}", key) for key in vb.CATEGORIES])
    assert battery.coverage_gaps() == []


def test_coverage_detects_category_without_probe() -> None:
    """漏一类不能是绿灯：这正是手写清单腐烂的方向。"""
    battery = vb.Battery(probes=[_probe(f"probe-{key}", key) for key in list(vb.CATEGORIES)[:-1]])
    gaps = battery.coverage_gaps()
    assert any("没有任何探针" in gap for gap in gaps)


def test_coverage_detects_unregistered_category() -> None:
    """探针声称属于一个规范未定义的类别：报红，不能默认算进某个已有类别。"""
    battery = vb.Battery(probes=[_probe(f"probe-{key}", key) for key in vb.CATEGORIES])
    battery.probes.append(_probe("probe-unknown", "C9"))
    gaps = battery.coverage_gaps()
    assert any("未登记的类别 C9" in gap for gap in gaps)


def test_coverage_detects_duplicate_check_id() -> None:
    battery = vb.Battery(probes=[_probe(f"probe-{key}", key) for key in vb.CATEGORIES])
    battery.probes.append(_probe("probe-C1", "C1"))
    assert any("核验编号重复" in gap for gap in battery.coverage_gaps())


# --------------------------------------------------------------------------- #
# 指标解析：拿不到数字就不能宣称已核验
# --------------------------------------------------------------------------- #
def test_harvest_metrics_reads_contract_check_output() -> None:
    output = "\n".join(
        [
            "[PASS] Action Registry ↔ OpenAPI 一致：100 个 action",
            "[PASS] 运行时 ↔ 契约覆盖：ACTIVE 56 个（已实现 35 / 登记未实现 21）",
            "[PASS] 事件契约：29 个事件 / 29 个发出点 / 显式缺口 0 项",
        ]
    )
    battery = vb.Battery()
    parsed = vb._harvest_metrics(battery, output)

    assert {"actions_total", "active_actions", "events_total"} <= parsed
    assert battery.metrics["implemented_actions"] == "35"
    assert battery.metrics["registered_not_implemented"] == "21"
    assert battery.metrics["events_emitted"] == "29"


def test_harvest_metrics_returns_empty_when_format_changed() -> None:
    """闸门换了输出格式 → 解析集合为空，调用方据此把状态降为 PARTIAL。"""
    battery = vb.Battery()
    assert vb._harvest_metrics(battery, "[PASS] 一切正常") == set()
    assert battery.metrics == {}


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("1201 passed in 45.2s", {"passed": 1201}),
        ("3 failed, 118 passed in 9s", {"failed": 3, "passed": 118}),
        ("2 errors, 7 passed in 1s", {"error": 2, "passed": 7}),
        ("no tests ran", {}),
    ],
)
def test_pytest_summary_parsing(line: str, expected: dict[str, int]) -> None:
    assert vb._parse_pytest_counts(line) == expected


# --------------------------------------------------------------------------- #
# 渲染
# --------------------------------------------------------------------------- #
def test_render_contains_expression_format_and_metrics(monkeypatch: pytest.MonkeyPatch) -> None:
    """核验表必须同时给出状态、证据位置与限制，而不是只写「PASS」。"""
    monkeypatch.setattr(vb, "_git_context", lambda: ("abc1234", "洁净"))
    battery = vb.Battery(
        probes=[_probe("C1.1", "C1")],
        metrics={"baseline_sha256": "deadbeef"},
        metric_sources={"baseline_sha256": "C1.1"},
    )
    # 用一套只含 C1 的类别表，避免「其余六类无探针」污染本次断言。
    monkeypatch.setattr(vb, "CATEGORIES", {"C1": "基准文件完整性"})
    rendered = vb.render(battery)

    assert "| 编号 | 类别 | 核验对象 | 核验状态 | 证据等级 | 证据位置 | 未完成事项 |" in rendered
    assert "`[V-VERIFIED]`" in rendered
    assert "限制：无" in rendered
    assert "`baseline_sha256`" in rendered
    assert "完备性判定：PASS" in rendered


# --------------------------------------------------------------------------- #
# pytest 状态判据：统计摘要缺失 ≠ 测试失败
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("code", "counts", "expected"),
    [
        (0, {}, vb.PARTIAL),  # -qq 抑制摘要：证据不足，不是代码坏
        (0, {"passed": 1201}, vb.VERIFIED),
        (0, {"passed": 118, "failed": 3}, vb.FAILED),
        (1, {"passed": 10, "error": 1}, vb.FAILED),
        (0, {"failed": 0}, vb.PARTIAL),
    ],
)
def test_pytest_state_separates_missing_evidence_from_failure(
    code: int, counts: dict[str, int], expected: str
) -> None:
    state, limitation = vb._pytest_state(code, counts)
    assert state == expected
    assert limitation, "任何状态都必须给出限制或依据，不能只回一个标签"


# --------------------------------------------------------------------------- #
# 测试目标派生：不得手写待扫清单
# --------------------------------------------------------------------------- #
def test_all_test_directories_are_registered() -> None:
    """仓库里任何含 test_*.py 的目录都必须归属某一层，否则核验表会漏检。"""
    _, unregistered = vb._discover_test_targets()
    assert unregistered == []


def test_e2e_and_architecture_are_both_covered() -> None:
    """回归用例：初版手写层清单漏了 tests/architecture，漏检还表现为绿灯。"""
    layers, _ = vb._discover_test_targets()
    covered = {path.name for paths in layers.values() for path in paths}
    assert {"architecture", "e2e", "unit"} <= covered


def test_every_test_file_belongs_to_exactly_one_layer() -> None:
    """双向完备：派生出的目标不重复、不遗漏文件系统里的测试文件。"""
    layers, _ = vb._discover_test_targets()
    targets = [path for paths in layers.values() for path in paths]
    assert len(targets) == len(set(targets))
    all_files = set(vb._TEST_ROOT.rglob("test_*.py"))
    covered_files = {f for target in targets for f in target.rglob("test_*.py")}
    assert all_files == covered_files


# --------------------------------------------------------------------------- #
# 宿主安全钩子：整次观测不可信，不能按解析出的 failed 数判代码失败
# --------------------------------------------------------------------------- #
def test_safe_delete_marker_blocks_even_with_parsed_failures() -> None:
    state, limitation = vb._pytest_observation_state(
        1,
        {"passed": 944, "error": 161},
        "[safe-delete][SAFE_DELETE_BULK_CONFIRM_REQUIRED] count=876",
    )
    assert state == vb.BLOCKED
    assert "安全钩子" in limitation
