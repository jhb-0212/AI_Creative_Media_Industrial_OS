"""QCReviewSession 状态机四副本一致性（契约 ↔ 枚举 ↔ 领域表 ↔ 迁移字面量）。

为什么需要这个文件：同一个状态链在本仓有**四份**独立副本，各自有存在的理由，
也各自可能单独漂移——

1. `contracts/enums/enum_registry.yaml`（取值集合）与
   `contracts/state_machines/state_machines.yaml`（转移链）：Phase 0 冻结产物，
   权威来源是正文 C.7；
2. `services/qc_governance/domain/enums.py`：运行时判定要知道有哪些状态；
3. `services/qc_governance/domain/state_machine.py`：运行时判定要知道能怎么走；
4. `data/migrations/versions/f1a2b3c4d5e6_*.py`：守卫触发器的转移表播种数据。

第 4 份**刻意不 import 第 2/3 份**——迁移是历史快照，若它随代码变化，重放同一个
revision 会得到结构不同的库。代价是"代码改了、迁移没跟上"这种漂移不会被任何
东西发现，除非有人显式比对。本文件就是那个显式比对。

同样地，`WAIVE_WITH_AUTHORITY` 的差异（登记为状态值、C.7 链里没有、也无转移边）
不靠"大家都记得"来维持，而是由 ``test_waive_with_authority_has_no_edges_anywhere``
把三份副本一起钉住。
"""

from __future__ import annotations

import importlib.util
from itertools import pairwise
from pathlib import Path
from typing import Any

import yaml

from services.qc_governance.domain.enums import (
    QCIssueHumanDecision,
    QCIssueSeverity,
    QCReviewSessionStatus,
)
from services.qc_governance.domain.state_machine import (
    QC_REVIEW_SESSION_ALLOWED_TRANSITIONS,
)
from services.qc_governance.repository.orm import SESSION_STATUS_VALUES

_ROOT = Path(__file__).resolve().parents[2]
_MIGRATION = (
    _ROOT / "data" / "migrations" / "versions" / "f1a2b3c4d5e6_create_qc_review_sessions.py"
)
_ENUM_REGISTRY = _ROOT / "contracts" / "enums" / "enum_registry.yaml"
_STATE_MACHINES = _ROOT / "contracts" / "state_machines" / "state_machines.yaml"


def _load_migration() -> Any:
    """按文件路径加载迁移模块（`versions/` 不是包，不能用 import 语句）。"""
    spec = importlib.util.spec_from_file_location("_qc_review_migration", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_yaml(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _contract_states() -> list[str]:
    """enum_registry 里 QCReviewSession 的登记取值。"""
    registry = _load_yaml(_ENUM_REGISTRY)
    for entry in registry["objects"]:
        if entry["object"] == "QCReviewSession":
            return list(entry["states"])
    raise AssertionError("enum_registry.yaml 未登记 QCReviewSession（契约被改动？）")


def _contract_chain_edges() -> set[tuple[str, str]]:
    """把 state_machines.yaml 的 `A→B→C` 文本链展开成边集合。

    取 ``authoritative_source: C.7`` 的那一条（该条目同时登记了 C.3 的冲突表述，
    见 ``other_definitions``——冲突本身由契约的 ``conflict`` 字段承载，测试只认
    权威版本，否则断言会把两套口径混成一锅）。
    """
    payload = _load_yaml(_STATE_MACHINES)
    entries = [
        entry for entry in payload["objects"] if entry["object"] == "QCReviewSession"
    ]
    assert len(entries) == 1, "state_machines.yaml 的 QCReviewSession 条目不唯一"
    entry = entries[0]
    assert entry["authoritative_source"].startswith("C.7"), (
        "QCReviewSession 的权威来源已不再是 C.7，请先更新 ADR-R36 再改这条断言"
    )
    hops = [hop.strip() for hop in entry["states"].split("→")]
    return set(pairwise(hops))


def _domain_edges() -> set[tuple[str, str]]:
    return {
        (src.value, tgt.value)
        for src, targets in QC_REVIEW_SESSION_ALLOWED_TRANSITIONS.items()
        for tgt in targets
    }


# --------------------------------------------------------------- 取值集合一致
def test_enum_matches_enum_registry() -> None:
    """代码枚举取值 == enum_registry 登记取值（缺值与多值都报红）。"""
    in_code = {member.value for member in QCReviewSessionStatus}
    in_contract = set(_contract_states())
    assert in_code == in_contract, (
        f"枚举与 enum_registry 不一致：仅代码有 {sorted(in_code - in_contract)}，"
        f"仅契约有 {sorted(in_contract - in_code)}"
    )


def test_migration_status_values_match_enum() -> None:
    """迁移 CHECK 的取值字面量 == 代码枚举（迁移是快照，不能 import 枚举）。"""
    module = _load_migration()
    seeded = set(module.QC_REVIEW_STATUS_VALUES)
    in_code = {member.value for member in QCReviewSessionStatus}
    assert seeded == in_code, (
        f"迁移取值与枚举不一致：仅迁移有 {sorted(seeded - in_code)}，"
        f"仅代码有 {sorted(in_code - seeded)}"
    )


def test_orm_status_values_match_enum() -> None:
    """ORM 的 CheckConstraint 字面量 == 代码枚举（第三处双写）。"""
    in_orm = set(SESSION_STATUS_VALUES)
    in_code = {member.value for member in QCReviewSessionStatus}
    assert in_orm == in_code, (
        f"ORM 取值与枚举不一致：仅 ORM 有 {sorted(in_orm - in_code)}，"
        f"仅代码有 {sorted(in_code - in_orm)}"
    )


# --------------------------------------------------------------- 转移边一致
def test_contract_chain_yields_domain_edges() -> None:
    """C.7 的线性链展开后，边集合必须与领域转移表逐边相等。

    这条断言同时保证了"链上没有旁路"：领域表多一条边（例如凭空加
    RESOLVED→CLOSED）会被这里报出来，少一条边同样。
    """
    contract_edges = _contract_chain_edges()
    domain_edges = _domain_edges()
    assert contract_edges == domain_edges, (
        f"契约链与领域转移表不一致：仅契约有 {sorted(contract_edges - domain_edges)}，"
        f"仅代码有 {sorted(domain_edges - contract_edges)}"
    )


def test_migration_transitions_match_domain_table() -> None:
    """迁移播种的转移边 == 领域转移表（数据库守卫与领域判定同源）。"""
    module = _load_migration()
    seeded = set(module.QC_REVIEW_STATUS_TRANSITIONS)
    domain_edges = _domain_edges()
    assert seeded == domain_edges, (
        f"迁移转移边与领域表不一致：仅迁移有 {sorted(seeded - domain_edges)}，"
        f"仅代码有 {sorted(domain_edges - seeded)}"
    )


def test_resolved_to_closed_is_absent_from_every_copy() -> None:
    """31.5 裁决 ④：RESOLVED→CLOSED 跳关在四份副本里都必须不存在。

    单独写一条而不是只靠上面两个集合相等：集合相等失败时报的是"哪条边多了"，
    而跳关是**红线**，值得有一条报错信息直接点出裁决依据的断言。
    """
    module = _load_migration()
    jump = ("RESOLVED", "CLOSED")
    assert jump not in _contract_chain_edges()
    assert jump not in _domain_edges()
    assert jump not in set(module.QC_REVIEW_STATUS_TRANSITIONS)
    # 反向确认"该有的那条边还在"——否则把 RESOLVED 的出边全删也能过上面三条。
    assert ("RESOLVED", "GATE_ELIGIBLE") in _domain_edges()


def test_waive_with_authority_has_no_edges_anywhere() -> None:
    """可表示不可进入：三份副本都不给它任何入边或出边。

    enum_registry 把它登记为状态，C.7 链里没有它，21.1 又把它当人工结论类型。
    这种"契约内部不一致但两边都得照做"的取值，唯一安全的实现是：保留取值
    （否则 enum_parity 报缺值）、不造边（否则凭空发明来源）。ADR-R36 §3 登记。
    """
    waive = QCReviewSessionStatus.WAIVE_WITH_AUTHORITY.value
    module = _load_migration()
    for src, tgt in _domain_edges() | set(module.QC_REVIEW_STATUS_TRANSITIONS):
        assert src != waive and tgt != waive, f"{waive} 不应有转移边，发现 {src}->{tgt}"
    assert waive not in {src for src, _ in _contract_chain_edges()}
    assert waive not in {tgt for _, tgt in _contract_chain_edges()}
    # 但取值必须在全部三份取值集合里（可表示）。
    assert waive in {member.value for member in QCReviewSessionStatus}
    assert waive in _contract_states()
    assert waive in set(module.QC_REVIEW_STATUS_VALUES)


# ----------------------------------------------------------- 不可变列白名单
def test_migration_mutable_whitelist_is_only_decisions_and_reviewers() -> None:
    """守卫只放行两列：结论追加与参与人登记。

    其余列一律不可变——尤其 ``findings`` / ``reference_set`` / ``qc_report_ref``
    / ``asset_version_id``：允许事后改输入，等于允许把"复核了什么"改写成另一个
    更好看的故事，C5 的证据链就此失效。
    """
    module = _load_migration()
    mutable = set(module._MUTABLE_COLUMNS)
    assert mutable == {"decisions", "reviewers"}
    immutable_content = {
        "entity_id",
        "entity_type",
        "schema_version",
        "owner_module",
        "tenant_id",
        "organization_id",
        "created_at",
        "created_by",
        "project_id",
        "asset_version_id",
        "qc_report_ref",
        "reference_set",
        "findings",
    }
    assert mutable.isdisjoint(immutable_content)


def test_migration_guard_covers_delete_and_unregistered_edges() -> None:
    """守卫函数体里必须同时有"禁删"与"未登记边拒绝"两段。

    这两条是数据库层的独立证据点（ADR-R20）：直连 SQL 绕过服务层时，只有
    触发器还在拦。断言源码文本不是为了测 SQL 拼写，而是防止有人"顺手简化"
    掉其中一段而全部应用层测试仍然绿。
    """
    source = _MIGRATION.read_text(encoding="utf-8")
    assert "TG_OP = 'DELETE'" in source
    assert "RAISE EXCEPTION" in source
    assert "from_status = old_status AND t.to_status = new_status" in source
    assert "BEFORE UPDATE OR DELETE" in source
    # 触发器与函数必须真的被创建（不是只写在 docstring 里）。
    assert "CREATE TRIGGER {TABLE}_guard_trigger" in source


# ---------------------------------------------------- 缺陷取值（4.3 / 21.1）
def test_issue_severity_matches_4_3() -> None:
    """4.3 的四个严重级别，且不含 7.6 的 BLOCKER（那是返工单级别）。"""
    assert {member.value for member in QCIssueSeverity} == {
        "CRITICAL",
        "MAJOR",
        "MINOR",
        "OBSERVATION",
    }
    assert not hasattr(QCIssueSeverity, "BLOCKER")


def test_issue_human_decision_matches_21_1() -> None:
    """21.1 的五个结论类型 + PENDING（8.9 缺陷结论未作出前的初值）。"""
    assert {member.value for member in QCIssueHumanDecision} == {
        "PENDING",
        "CONFIRM_DEFECT",
        "ARTISTIC_INTENTIONAL",
        "FALSE_POSITIVE",
        "NEEDS_MORE_EVIDENCE",
        "WAIVE_WITH_AUTHORITY",
    }
