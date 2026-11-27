"""#41 guard 的执行顺序契约：五表全部加锁之后才允许任何 count。

审查要求：不能只看日志，必须检查 guard 生成语句的实际顺序。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATION = REPO_ROOT / "data" / "migrations" / "versions" / "a0b1c2d3e4f5_domain_split_r16.py"


def _module():
    spec = importlib.util.spec_from_file_location("wp04_guard_order", MIGRATION)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_all_locks_precede_any_count() -> None:
    mod = _module()
    sql = mod._cutover_guard_sql()
    lock_positions = [sql.index(f"LOCK TABLE project_planning.{t} IN ACCESS EXCLUSIVE MODE") for t in mod._CUTOVER_TABLES]
    assert lock_positions == sorted(lock_positions), "锁必须按固定顺序取得"
    first_count = sql.index("count(*)")
    assert max(lock_positions) < first_count, "必须五表全部加锁后才开始 count"


def test_lock_order_matches_declared_tuple() -> None:
    mod = _module()
    sql = mod._cutover_guard_sql()
    locks = [line.split("LOCK TABLE project_planning.")[1].split(" ")[0]
             for line in sql.splitlines() if "LOCK TABLE" in line]
    assert tuple(locks) == mod._CUTOVER_TABLES


def test_non_empty_raises_and_never_truncates() -> None:
    mod = _module()
    sql = mod._cutover_guard_sql().upper()
    assert "RAISE EXCEPTION" in sql
    for forbidden in ("TRUNCATE", "DELETE FROM", "DROP TABLE IF EXISTS"):
        assert forbidden not in sql
