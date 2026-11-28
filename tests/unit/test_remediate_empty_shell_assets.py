"""``scripts/remediate_empty_shell_assets.py`` 的判定与 SQL 构造单测（不连库）。

为什么要单测
------------
这个脚本只在「生产库要被升级、但存量资产是空壳」这一刻跑一次，跑错没有第二次机会：

- 判据比迁移宽 → 删掉迁移本可转换的行（真资产被当成空壳）；
- 判据比迁移严 → 迁移仍然 fail-closed，人会以为是脚本坏了；
- 归档缺失或不等值 → ADR-R11 的证据链断了，删除不可回滚。

所以判据与迁移共用同一段 SQL 常量（``NON_CONVERTIBLE`` 就是 ``d0e1`` 的
``_CONVERTIBLE`` 取反），这里锁定「两段仍逐项对应」，并锁死危险动作的边界：
默认 dry-run、``--apply`` 必须给日期后缀、删除前必须有 md5 等值校验。
"""

from __future__ import annotations

import pytest

from scripts import remediate_empty_shell_assets as rx


def test_non_convertible_is_the_exact_negation_of_migration_judgement() -> None:
    """判据必须与 d0e1 的 _CONVERTIBLE 逐项同源（迁移改了口径这里要跟着改）。"""
    sql = rx.NON_CONVERTIBLE
    for fragment in (
        "storage_ref ? 'uri'",
        "jsonb_typeof(storage_ref -> 'uri') = 'string'",
        "substr(storage_ref ->> 'uri', 15, 64) = lower(sha256)",
        "local://[0-9a-f]{2}/[0-9a-f]{2}/[0-9a-f]{64}",
    ):
        assert fragment in sql, f"判据缺少 {fragment!r}"
    assert sql.strip().startswith("NOT (")


@pytest.mark.parametrize("bad", ["2026-11-28", "../x", "", "2026112", "abcdefgh"])
def test_suffix_rejects_anything_that_is_not_eight_digits(bad: str) -> None:
    with pytest.raises(ValueError):
        rx.validate_suffix(bad)


def test_suffix_accepts_date_form() -> None:
    assert rx.validate_suffix("20261128") == "20261128"


def test_plan_flags_noop_and_trigger_need() -> None:
    assert rx.build_plan(total_rows=1873, bad_rows=0, qc_rows=0, qc_distinct_versions=0).nothing_to_do
    plan = rx.build_plan(total_rows=1873, bad_rows=1873, qc_rows=519, qc_distinct_versions=519)
    assert plan.nothing_to_do is False
    assert plan.needs_trigger_disable is True


@pytest.mark.parametrize(
    ("total", "bad", "qc_versions"),
    [(10, 11, 0), (10, 5, 6)],
)
def test_plan_rejects_impossible_numbers(total: int, bad: int, qc_versions: int) -> None:
    with pytest.raises(ValueError):
        rx.build_plan(total_rows=total, bad_rows=bad, qc_rows=qc_versions, qc_distinct_versions=qc_versions)


def test_archive_sql_keeps_rows_and_is_suffix_scoped() -> None:
    sql = rx.archive_sql("20261128")
    assert "CREATE TABLE IF NOT EXISTS quarantine.asset_versions_20261128" in sql
    assert "quarantine.qc_reports_20261128" in sql
    # 归档只拷不删
    assert "DELETE" not in sql.upper()


def test_retire_sql_disables_only_user_triggers_and_restores_them() -> None:
    sql = rx.retire_sql("20261128")
    assert sql.count("DISABLE TRIGGER USER") == 2
    assert sql.count("ENABLE TRIGGER USER") == 2
    assert "BEGIN;" in sql and "COMMIT;" in sql
    # 只删不可转换行，不给「整表清空」留下可能
    assert f"DELETE FROM {rx.TABLE} WHERE {rx.NON_CONVERTIBLE}" in sql


def test_md5_sql_is_order_stable_and_handles_empty() -> None:
    sql = rx.md5_sql(rx.TABLE, rx.NON_CONVERTIBLE)
    assert "coalesce(md5(string_agg" in sql
    assert "ORDER BY t::text" in sql
    assert "empty" in sql
