"""B2 证据映射：AGENT-NEG-001～004 ↔ 真实负向用例。

为什么要有这个文件（而不是在证据文档里手写一张表）：
`Phase1-实现验收清单.md` 的 B2 要求「AGENT-NEG-001～004 四条负向测试执行记录」。
用例本来就有，缺的是**编号绑定**——而手写的编号表在下一次改名/删用例时不会报错，
会安静地退化成"文档说有、实际没有"。这里把绑定做成可校验的事实：

1. 编号集合从**基线正文**（31.6 负向测试表）解析，不在本文件里硬编码——
   基线加一条 AGENT-NEG-005，这个文件立刻红，逼人补证据；
2. 每条编号必须**要么**指向真实用例（`_EVIDENCE`），**要么**显式登记为阻塞
   （`_BLOCKED`，必须附原因）——不允许两处都空、也不允许两处都填；
3. 指向的用例必须真的带 `@pytest.mark.agent_negative("<该编号>")`——用源码
   扫描校验，不做 import（避免为了校验编号而拉起重依赖）；
4. 反向也校验：任何被标注的用例都必须出现在 `_EVIDENCE` 里，防止"标了但没登记"。

阻塞不是"通过"。`AGENT-NEG-004`（Agent 调用超预算/超配额动作）在 Phase 1 无
可执行载体：`services/budget_quota` 仍是空壳（只有 `.gitkeep`），
`GenerationTask` 只做 `budget_reservation_ref` / `quota_reservation_ref` 的
**结构性**准入，数值级超限判定不存在（ADR-R32 债务）。因此它被登记为
`BLOCKED` 而不是伪装成"已覆盖"。
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BASELINE = REPO_ROOT / "docs" / "baseline" / "AI影视工业化生产平台V2.0.5.md"
PYPROJECT = REPO_ROOT / "pyproject.toml"

#: 基线 31.6 负向测试表的行格式：`| AGENT-NEG-001 | <场景> | <通过标准> |`。
_ROW = re.compile(r"^\|\s*(AGENT-NEG-\d{3})\s*\|([^|]*)\|([^|]*)\|\s*$", re.MULTILINE)

#: 编号 → 承载该编号的真实用例（路径相对仓库根，`::` 后是函数名）。
#: 一个编号可以有多条：B2 是跨层红线（授权层 / 领域层 / 数据库层），
#: 只留一条会让"某一层被改坏"不被发现。
_EVIDENCE: dict[str, tuple[str, ...]] = {
    "AGENT-NEG-001": (
        # 授权层：Agent 持导演角色仍不得做 CRITICAL 裁决（走真实 HTTP 入口）
        "tests/unit/test_gate_api.py::test_agent_cannot_approve_even_with_director_role",
        # 授权策略：职责分离（申请人 ≠ 审批人）
        "tests/permission/test_authorization_policy.py::test_self_approval_is_denied",
        # 授权策略：全量扫描——没有任何 CRITICAL 动作对 Agent 可执行
        "tests/permission/test_authorization_policy.py::test_no_critical_action_is_executable_by_agent",
    ),
    "AGENT-NEG-002": (
        # 越权发布：Agent 持「交付负责人」角色仍不得 RELEASE
        "tests/unit/test_delivery_release_api.py::test_agent_cannot_release_critical",
        # Agent 只可产 DRAFT：能建草案、不得确认正式修订
        "tests/integration/test_shot_planning_api.py::test_agent_cannot_confirm_revision",
    ),
    "AGENT-NEG-003": (
        # 直写 Gate：绕过全部应用代码，数据库 CHECK 仍拒绝 Agent 作为决策人
        "tests/integration/test_gate_decision_immutability.py::test_agent_decider_is_rejected_by_check",
        # 直写审批票：数据库 CHECK 拒绝 Agent 投票
        "tests/integration/test_workflow_approval_invariants.py::test_agent_voter_rejected_by_check",
    ),
}

#: 编号 → 阻塞原因（含解锁条件）。非空即表示"没有可执行载体"，不是"通过"。
_BLOCKED: dict[str, str] = {
    "AGENT-NEG-004": (
        "无可执行载体：services/budget_quota 仍是空壳（仅 .gitkeep），"
        "GenerationTask 只做 budget_reservation_ref / quota_reservation_ref 的"
        "结构性非空校验，数值级超预算/超配额判定不存在（ADR-R32 债务）。"
        "解锁条件：budget_quota 域实现 QuotaReservation 与预算策略校验后，"
        "补一条「Agent 请求超配额 → 结构化错误码而非 500」的用例。"
    ),
}


def _baseline_cases() -> list[tuple[str, str, str]]:
    """解析基线负向测试表，返回 [(编号, 场景, 通过标准)]。"""
    text = BASELINE.read_text(encoding="utf-8")
    return [(m.group(1), m.group(2).strip(), m.group(3).strip()) for m in _ROW.finditer(text)]


def _marked_nodes() -> dict[tuple[str, str], str]:
    """扫描源码，返回 {(相对路径, 函数名): 标记里的编号}。

    用文本扫描而不是 import：这些用例模块依赖 DB 会话与 FastAPI 装配，
    为了核对编号把它们全部 import 一遍既慢又会引入非必要副作用。
    代价是"标记必须紧邻函数定义"——下面用正则强制这个形态。
    """
    pattern = re.compile(
        r'@pytest\.mark\.agent_negative\(\s*"([^"]+)"\s*\)\s*\n(?:@[^\n]*\n)*\s*'
        r"(?:async\s+)?def\s+(\w+)\s*\(",
    )
    found: dict[tuple[str, str], str] = {}
    for path in sorted((REPO_ROOT / "tests").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "agent_negative" not in text:
            continue
        rel = path.relative_to(REPO_ROOT).as_posix()
        for match in pattern.finditer(text):
            found[(rel, match.group(2))] = match.group(1)
    return found


def test_baseline_declares_exactly_the_four_agent_cases() -> None:
    """基线必须仍有 AGENT-NEG-001～004 四条（集合变了就要重新对齐证据）。"""
    cases = _baseline_cases()
    assert cases, "基线 31.6 负向测试表解析为空——正则或正文结构变了"
    ids = [case[0] for case in cases]
    assert ids == ["AGENT-NEG-001", "AGENT-NEG-002", "AGENT-NEG-003", "AGENT-NEG-004"], (
        f"基线负向表编号集合变化（当前 {ids}）：请同步更新 _EVIDENCE / _BLOCKED"
    )
    # 每条都要有可读的场景与通过标准：解析不到说明表格列数变了。
    assert all(scene and verdict for _, scene, verdict in cases)


def test_every_case_is_covered_or_explicitly_blocked() -> None:
    """每条编号要么有用例、要么有阻塞原因；不允许静默缺口。"""
    baseline_ids = {case[0] for case in _baseline_cases()}
    covered = set(_EVIDENCE)
    blocked = set(_BLOCKED)

    unaligned = baseline_ids - covered - blocked
    assert not unaligned, f"以下编号既无证据也未登记阻塞: {sorted(unaligned)}"

    both = covered & blocked
    assert not both, f"以下编号同时登记了证据与阻塞（口径矛盾）: {sorted(both)}"

    unknown = (covered | blocked) - baseline_ids
    assert not unknown, f"以下编号不在基线负向表中: {sorted(unknown)}"


def test_covered_cases_exist_and_carry_their_own_case_id() -> None:
    """每条证据必须指向真实用例，且用例标注的编号与登记一致。"""
    marked = _marked_nodes()
    problems: list[str] = []
    for case_id, nodes in _EVIDENCE.items():
        assert nodes, f"{case_id} 的证据列表为空"
        for node in nodes:
            path, _, func = node.partition("::")
            assert func, f"{case_id} 的节点缺少函数名: {node}"
            actual = marked.get((path, func))
            if actual is None:
                problems.append(f"{node} 未标 @pytest.mark.agent_negative({case_id!r})")
            elif actual != case_id:
                problems.append(f"{node} 标注为 {actual}，登记为 {case_id}")
    assert not problems, "证据映射与源码标注不一致：\n" + "\n".join(problems)


def test_no_marked_case_is_missing_from_the_registry() -> None:
    """反向校验：标了编号的用例必须登记，避免"标了但没人引用"的漂移。"""
    registered = {(node.partition("::")[0], node.partition("::")[2]) for nodes in _EVIDENCE.values() for node in nodes}
    stray = [
        f"{path}::{func} -> {case_id}"
        for (path, func), case_id in _marked_nodes().items()
        if (path, func) not in registered
    ]
    assert not stray, "以下用例标注了 AGENT-NEG 编号但未在 _EVIDENCE 登记：\n" + "\n".join(stray)


def test_blocked_cases_state_a_reason_and_an_unlock_condition() -> None:
    """阻塞必须写清"为什么"和"什么条件下解除"，否则等于没登记。"""
    for case_id, reason in _BLOCKED.items():
        assert len(reason) >= 40, f"{case_id} 的阻塞原因过于笼统: {reason!r}"
        assert "解锁条件" in reason, f"{case_id} 未写解锁条件"


def test_agent_negative_marker_is_registered_in_pyproject() -> None:
    """marker 必须注册：未注册的 marker 只会产生警告，不会被当配置问题对待。"""
    # pyproject.toml 是 TOML 不是 YAML：用 tomllib 解析（用 yaml 会把它读成
    # 单个标量，然后"注册与否"变成永远 False 的假断言）。
    config = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    markers = config["tool"]["pytest"]["ini_options"]["markers"]
    assert any(str(item).startswith("agent_negative(") for item in markers), (
        f"pyproject.toml 的 markers 未登记 agent_negative: {markers}"
    )
