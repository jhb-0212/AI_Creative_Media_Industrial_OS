"""可逆性矩阵 V2 判据用例：集合比对必须真的抓出不对称，也不能制造假阳性。"""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import reversibility_matrix as rm  # noqa: E402


def _row(rev: str, direction: str, sql: str, operation: str, roles: list[str] | None = None) -> dict:
    return {
        "revision": rev,
        "chain_index": 0,
        "direction": direction,
        "operation": operation,
        "roles": roles or [],
        "object": "",
        "source": "runtime",
        "sql": sql,
    }


def _inventory(rows: list[dict]) -> dict[str, object]:
    per_rev: dict[str, dict[str, dict[str, int]]] = {}
    for row in rows:
        bucket = per_rev.setdefault(row["revision"], {"upgrade": {}, "downgrade": {}})
        bucket[row["direction"]][row["operation"]] = bucket[row["direction"]].get(row["operation"], 0) + 1
    return {"chain_length": 1, "head": "head", "rows": rows, "per_revision": per_rev}


def test_grant_and_revoke_same_set_is_symmetric() -> None:
    payload = rm.build_matrix(
        _inventory(
            [
                _row("r1", "upgrade", "GRANT SELECT ON project_planning.projects TO app_role", "grant"),
                _row("r1", "downgrade", "REVOKE SELECT ON project_planning.projects FROM app_role", "revoke"),
            ]
        )
    )
    assert payload["matrix"][0]["verdict"] == "symmetric"


def test_grant_on_other_role_is_not_a_match() -> None:
    """数量相等但角色不同 —— V1 会误判对称，V2 必须抓出来。"""
    payload = rm.build_matrix(
        _inventory(
            [
                _row("r1", "upgrade", "GRANT SELECT ON t TO app_role", "grant"),
                _row("r1", "downgrade", "REVOKE SELECT ON t FROM other_role", "revoke"),
            ]
        )
    )
    assert payload["matrix"][0]["verdict"] == "asymmetric"
    assert any("missing revoke" in item for item in payload["matrix"][0]["findings"])


def test_revoke_of_existing_grant_without_restore_is_risk() -> None:
    payload = rm.build_matrix(
        _inventory(
            [
                _row("r1", "upgrade", "REVOKE UPDATE ON t FROM app_role", "revoke"),
                _row("r1", "downgrade", "GRANT SELECT ON t TO app_role", "grant"),
            ]
        )
    )
    assert any("missing restore" in item for item in payload["matrix"][0]["findings"])


def test_downgrade_extra_grant_is_widening() -> None:
    payload = rm.build_matrix(
        _inventory(
            [
                _row("r1", "upgrade", "REVOKE SELECT ON t FROM app_role", "revoke"),
                _row("r1", "downgrade", "GRANT SELECT, INSERT ON t TO app_role", "grant"),
            ]
        )
    )
    assert any("extra grant" in item for item in payload["matrix"][0]["findings"])


def test_grant_option_mismatch_is_asymmetric() -> None:
    payload = rm.build_matrix(
        _inventory(
            [
                _row("r1", "upgrade", "GRANT SELECT ON t TO app_role WITH GRANT OPTION", "grant"),
                _row("r1", "downgrade", "REVOKE SELECT ON t FROM app_role", "revoke"),
            ]
        )
    )
    assert payload["matrix"][0]["verdict"] == "asymmetric"


def test_revoke_all_covers_specific_grant() -> None:
    payload = rm.build_matrix(
        _inventory(
            [
                _row("r1", "upgrade", "GRANT SELECT, INSERT ON t TO app_role", "grant"),
                _row("r1", "downgrade", "REVOKE ALL ON t FROM app_role", "revoke"),
            ]
        )
    )
    assert payload["matrix"][0]["verdict"] == "symmetric"


def test_all_tables_revoke_covers_table_grant() -> None:
    payload = rm.build_matrix(
        _inventory(
            [
                _row("r1", "upgrade", "GRANT SELECT ON audit.audit_logs TO project_planning_role", "grant"),
                _row(
                    "r1",
                    "downgrade",
                    "REVOKE ALL ON ALL TABLES IN SCHEMA audit FROM project_planning_role",
                    "revoke",
                ),
            ]
        )
    )
    assert payload["matrix"][0]["verdict"] == "symmetric"


def test_created_role_without_drop_is_identity_leak() -> None:
    payload = rm.build_matrix(
        _inventory([_row("r1", "upgrade", "CREATE ROLE budget_quota_role", "create_role")])
    )
    assert any("身份泄漏" in item for item in payload["matrix"][0]["findings"])


def test_declared_retired_role_is_not_flagged() -> None:
    """qc_worker 已在 RETIRED_ROLES 登记，其重建走受控恢复，不算不对称。"""
    payload = rm.build_matrix(
        _inventory([_row("r1", "downgrade", "CREATE ROLE qc_worker", "create_role")])
    )
    assert payload["matrix"][0]["verdict"] == "symmetric"
    assert any("已登记退役" in item for item in payload["matrix"][0]["notes"])


def test_unclassified_asymmetry_blocks_gate() -> None:
    payload = rm.build_matrix(
        _inventory([_row("deadbeef0001", "upgrade", "GRANT SELECT ON t TO app_role", "grant")])
    )
    assert payload["unclassified_revisions"] == ["deadbeef0001"]


def test_matrix_orders_by_chain_index() -> None:
    rows = [
        {**_row("later", "upgrade", "GRANT SELECT ON t TO app_role", "grant"), "chain_index": 9},
        {**_row("earlier", "upgrade", "GRANT SELECT ON t TO app_role", "grant"), "chain_index": 3},
    ]
    payload = rm.build_matrix(_inventory(rows))
    indexes = [item["chain_index"] for item in payload["matrix"]]
    assert indexes == sorted(indexes)


def _matrix_row(rev: str, classification: str | None, verdict: str = "asymmetric") -> dict:
    return {"revision": rev, "classification": classification, "verdict": verdict, "findings": []}


def _payload(*rows: dict) -> dict:
    return {"matrix": list(rows)}


def _entry(rev: str, verdict: str, **extra: object) -> dict:
    return {"revision": rev, "parent": "parent-" + rev, "verdict": verdict, **extra}


def test_snapshot_clean_entries_pass() -> None:
    snap = {"results": [_entry("r1", "no_restore_proven")]}
    out = rm.evaluate_snapshot(snap, _payload(_matrix_row("r1", "no_restore_proven")))
    assert out["blocks"] == []
    assert out["counts"]["no_restore_proven"] == 1


def test_snapshot_unexplained_diff_blocks_even_if_declared_no_restore() -> None:
    snap = {"results": [_entry("r1", "no_restore_proven", diff={"acl": ["+DEFACL x:r:owner"]})]}
    out = rm.evaluate_snapshot(snap, _payload(_matrix_row("r1", "no_restore_proven")))
    assert any("未解释差异" in b for b in out["blocks"])


def test_snapshot_registered_ignore_rule_is_accepted_and_recorded() -> None:
    snap = {
        "results": [
            _entry(
                "r1",
                "no_restore_proven",
                diff={"rels": ["pg_toast_113463"]},
                ignored=[{"rule": "pg_toast_oid", "item": "pg_toast_113463"}],
            )
        ]
    }
    out = rm.evaluate_snapshot(snap, _payload(_matrix_row("r1", "no_restore_proven")))
    assert out["blocks"] == []
    assert out["ignored"]


def test_snapshot_unknown_ignore_rule_does_not_swallow_diff() -> None:
    snap = {
        "results": [
            _entry(
                "r1",
                "no_restore_proven",
                diff={"acl": ["+NS auth"]},
                ignored=[{"rule": "whatever", "item": "[acl] +NS auth"}],
            )
        ]
    }
    out = rm.evaluate_snapshot(snap, _payload(_matrix_row("r1", "no_restore_proven")))
    assert any("未解释差异" in b for b in out["blocks"])


def test_snapshot_restore_required_and_unproven_block() -> None:
    snap = {"results": [_entry("r1", "restore_required"), _entry("r2", "unproven_blocking")]}
    out = rm.evaluate_snapshot(
        snap, _payload(_matrix_row("r1", "restore_required"), _matrix_row("r2", "unproven_blocking"))
    )
    joined = " | ".join(out["blocks"])
    assert "restore_required" in joined
    assert "unproven_blocking" in joined


def test_snapshot_one_way_needs_safety_contract() -> None:
    snap = {"results": [_entry("r1", "intentional_one_way")]}
    out = rm.evaluate_snapshot(snap, _payload(_matrix_row("r1", "intentional_one_way")))
    assert any("safety_contract" in b for b in out["blocks"])
    snap["results"][0]["safety_contract"] = "ADR-R46: 收权不可回退，降级只保留结构"
    out2 = rm.evaluate_snapshot(snap, _payload(_matrix_row("r1", "intentional_one_way")))
    assert out2["blocks"] == []


def test_snapshot_must_cover_source_restore_required() -> None:
    snap = {"results": [_entry("r2", "no_restore_proven")]}
    out = rm.evaluate_snapshot(snap, _payload(_matrix_row("r1", "restore_required"), _matrix_row("r2", "no_restore_proven")))
    assert any("快照未证明修复" in b for b in out["blocks"])


def test_snapshot_revision_outside_chain_blocks() -> None:
    snap = {"results": [_entry("ghost", "no_restore_proven")]}
    out = rm.evaluate_snapshot(snap, _payload(_matrix_row("r1", "no_restore_proven")))
    assert any("不在迁移链内" in b for b in out["blocks"])


def test_snapshot_ignore_rules_are_narrow() -> None:
    assert set(rm.SNAPSHOT_IGNORE_RULES) == {"pg_toast_oid", "acl_item_order"}

def test_snapshot_missing_entry_blocks_even_if_classified() -> None:
    snap = {"results": [_entry("r2", "no_restore_proven")]}
    out = rm.evaluate_snapshot(
        snap, _payload(_matrix_row("r1", "no_restore_proven"), _matrix_row("r2", "no_restore_proven"))
    )
    assert any("缺少 P/D 往返快照证据" in b for b in out["blocks"])
