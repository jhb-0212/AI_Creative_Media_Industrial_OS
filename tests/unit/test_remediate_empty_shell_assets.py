"""``scripts/remediate_empty_shell_assets.py`` 的判定与语句构造单测（不连库）。

为什么这些断言值钱
------------------
这个脚本只在「生产库要被升级、但存量资产是空壳」这一刻跑一次，跑错没有第二次机会：

- 判据比迁移宽 → 删掉迁移本可转换的真资产；
- 判据比迁移严 → 迁移仍然 fail-closed，人会以为是脚本坏了；
- 归档缺失或不等值 → ADR-R11 的证据链断了，删除不可回滚；
- 删除按谓词而不是归档主键 → 归档之后新写入的行会被删掉但从未归档。

所以这里锁死三件事：判据**从迁移导入**（不是抄一份字面量）、危险动作成对、
删除只认归档主键。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from scripts import remediate_empty_shell_assets as rx


def _migration_convertible() -> str:
    migration = (
        Path(__file__).resolve().parents[2]
        / "data"
        / "migrations"
        / "versions"
        / "d0e1f2a3b4c5_storage_ref_controlled_locator.py"
    )
    spec = importlib.util.spec_from_file_location("_d0e1_for_test", migration)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module._CONVERTIBLE


def test_predicate_is_the_negation_of_the_migration_judgement_with_null_closed() -> None:
    """判据与迁移同源，且把 NULL 收进「不可转换」而不是留成第三个状态。

    ``NOT (expr)`` 在 ``expr`` 为 NULL 时结果也是 NULL：``WHERE NOT (...)`` 和
    ``WHERE NON_CONVERTIBLE`` 都会静默跳过这类行。``storage_ref`` 现在确实是
    NOT NULL，但判据不该由另一张表的约束来保证「没有第三种状态」。
    """
    assert f"NOT (COALESCE(({_migration_convertible()}), false))" == rx.NON_CONVERTIBLE


def test_predicate_module_reads_the_single_source() -> None:
    """构造函数必须真的读迁移文件，而不是返回一份抄来的常量。"""
    assert rx.non_convertible_predicate() == rx.NON_CONVERTIBLE
    assert "local://" in rx.NON_CONVERTIBLE
    assert "substr(storage_ref ->> 'uri', 15, 64) = lower(sha256)" in rx.NON_CONVERTIBLE


@pytest.mark.parametrize("bad", ["2026-11-28", "../x", "", "2026112", "abcdefgh"])
def test_suffix_rejects_anything_that_is_not_eight_digits(bad: str) -> None:
    with pytest.raises(ValueError):
        rx.validate_suffix(bad)


def test_suffix_accepts_date_form() -> None:
    assert rx.validate_suffix("20261128") == "20261128"


def test_plan_flags_noop_and_trigger_need() -> None:
    assert rx.build_plan(
        total_rows=1873, bad_rows=0, qc_rows=0, qc_distinct_versions=0
    ).nothing_to_do
    plan = rx.build_plan(total_rows=1873, bad_rows=1873, qc_rows=519, qc_distinct_versions=519)
    assert plan.nothing_to_do is False
    assert plan.needs_trigger_disable is True


@pytest.mark.parametrize(
    ("total", "bad", "qc_versions"),
    [(10, 11, 0), (10, 5, 6)],
)
def test_plan_rejects_impossible_numbers(total: int, bad: int, qc_versions: int) -> None:
    with pytest.raises(ValueError):
        rx.build_plan(
            total_rows=total, bad_rows=bad, qc_rows=qc_versions, qc_distinct_versions=qc_versions
        )


def test_archive_statements_lock_both_tables_and_never_delete() -> None:
    sql = "\n".join(rx.archive_statements("20261128"))
    assert f"LOCK TABLE {rx.TABLE} IN SHARE ROW EXCLUSIVE MODE" in sql
    assert f"LOCK TABLE {rx.QC_TABLE} IN SHARE ROW EXCLUSIVE MODE" in sql
    assert "quarantine.asset_versions_20261128" in sql
    assert "quarantine.qc_reports_20261128" in sql
    assert "DELETE" not in sql.upper()
    # 不复用旧归档表：IF NOT EXISTS 会让校验拿旧数据比新 live
    assert "IF NOT EXISTS" not in sql


def test_retire_deletes_by_archive_key_not_by_predicate() -> None:
    """删除只认归档里的主键；按谓词重扫会让「归档后新写入的行」被误删。"""
    statements = rx.retire_statements("20261128", qc_rows=519)
    delete_versions = [s for s in statements if s.startswith(f"DELETE FROM {rx.TABLE}")]
    delete_qc = [s for s in statements if s.startswith(f"DELETE FROM {rx.QC_TABLE}")]
    assert len(delete_versions) == 1 and len(delete_qc) == 1
    assert (
        "entity_id IN (SELECT entity_id FROM quarantine.asset_versions_20261128)"
        in delete_versions[0]
    )
    assert (
        "qc_report_id IN (SELECT qc_report_id FROM quarantine.qc_reports_20261128)" in delete_qc[0]
    )
    assert rx.NON_CONVERTIBLE not in delete_versions[0]


def test_retire_disables_and_restores_user_triggers_in_pairs() -> None:
    statements = rx.retire_statements("20261128", qc_rows=519)
    assert statements.count(f"ALTER TABLE {rx.QC_TABLE} DISABLE TRIGGER USER") == 1
    assert statements.count(f"ALTER TABLE {rx.QC_TABLE} ENABLE TRIGGER USER") == 1
    assert statements.count(f"ALTER TABLE {rx.TABLE} DISABLE TRIGGER USER") == 1
    assert statements.count(f"ALTER TABLE {rx.TABLE} ENABLE TRIGGER USER") == 1
    # 禁用 → 删除 → 恢复，顺序不能颠倒（先删后禁等于把保护面白关一次）
    order = [s.split(" ")[0:3] for s in statements]
    assert order[0] == ["ALTER", "TABLE", rx.QC_TABLE]
    assert statements.index(f"ALTER TABLE {rx.QC_TABLE} ENABLE TRIGGER USER") < statements.index(
        f"ALTER TABLE {rx.TABLE} DISABLE TRIGGER USER"
    )


def test_retire_skips_qc_trigger_when_no_qc_rows() -> None:
    """没有 QC 证据行就不该动 QC 表触发器（保护面不扩大）。"""
    statements = rx.retire_statements("20261128", qc_rows=0)
    assert all(rx.QC_TABLE not in s for s in statements)
    assert f"ALTER TABLE {rx.TABLE} DISABLE TRIGGER USER" in statements


def test_count_deleted_asks_the_database_for_the_real_rowcount() -> None:
    sql = rx.count_deleted(f"DELETE FROM {rx.TABLE} WHERE entity_id = 'x'")
    assert sql.startswith("WITH d AS (")
    assert "RETURNING 1" in sql
    assert sql.endswith("SELECT count(*) FROM d")


def test_fingerprint_uses_sha256_and_is_order_stable() -> None:
    sql = rx.fingerprint_sql(rx.TABLE, rx.NON_CONVERTIBLE)
    assert "sha256(convert_to(" in sql
    assert "ORDER BY t::text" in sql
    assert "empty" in sql


def test_md5_remains_as_compatibility_fingerprint() -> None:
    sql = rx.md5_sql(rx.TABLE, rx.NON_CONVERTIBLE)
    assert "coalesce(md5(string_agg" in sql
    assert "ORDER BY t::text" in sql


def test_set_difference_restricts_both_sides() -> None:
    sql = rx.set_difference_sql(rx.QC_TABLE, "quarantine.qc_reports_20261128", left_where="1=1")
    assert "EXCEPT" in sql
    assert "WHERE 1=1" in sql
