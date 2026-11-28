"""约束来源标记的契约用例：标记必须可核对，未标记必须保守，不得静默删除。

判据来自 WP-04 整改轮裁决：``COMMENT ON CONSTRAINT`` 方案可以使用，但要成为
**兼容性契约**，而不只是"实现细节"。
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
HELPER = REPO / "packages" / "shared" / "migration_constraints.py"
MIGRATIONS = [
    REPO / "data" / "migrations" / "versions" / "a13b0c1d2e3f_scene_shot_rework_runtime_tables.py",
    REPO / "data" / "migrations" / "versions" / "c3d4e5f6a7b8_shot_spec_runtime_tables.py",
]


def test_helper_returns_sql_and_does_not_execute() -> None:
    text = HELPER.read_text(encoding="utf-8")
    body = text.split('"""')
    code = body[2] if len(body) > 2 else text
    assert "op.execute(" not in code, "辅助模块不得直接执行 DDL（静态审计没有 op 代理）"
    assert "def mark_created_constraint" in text
    assert "def drop_marked_constraint" in text


def test_marker_contains_revision_and_is_queryable() -> None:
    text = HELPER.read_text(encoding="utf-8")
    assert 'MARK_PREFIX = "wp04-created-by:"' in text
    assert "COMMENT ON CONSTRAINT" in text
    # 标记必须落在 pg_description 上，才能按"来源"而不是按"名字"判定
    assert "pg_description" in text


def test_unmarked_constraint_is_preserved_not_dropped() -> None:
    text = HELPER.read_text(encoding="utf-8")
    assert "ELSIF marker IS NOT NULL" in text
    assert "RAISE NOTICE" in text
    assert "EXECUTE format(" in text and "DROP CONSTRAINT" in text
    # 未标记（marker 为 NULL）不进入删除/阻断分支
    assert "marker = '{marker}'" in text


def test_marker_definition_mismatch_blocks_instead_of_blind_drop() -> None:
    text = HELPER.read_text(encoding="utf-8")
    assert "actual_columns" in text, "必须按 conkey 还原列序"
    assert "RAISE EXCEPTION" in text, "标记与定义不一致时必须阻断"
    assert "contype" in text and "'u'" in text, "只允许删除 UNIQUE 约束"
    # 阻断分支不能同时执行 ALTER TABLE ... DROP CONSTRAINT
    guard = re.search(r"ELSE\s+RAISE EXCEPTION.*?END IF;", text, re.S)
    assert guard and "DROP CONSTRAINT" not in guard.group(0)


def test_migrations_execute_helper_sql_explicitly() -> None:
    for path in MIGRATIONS:
        text = path.read_text(encoding="utf-8")
        assert re.search(r"op\.execute\(\s*mark_created_constraint", text), (
            f"{path.name}: upgrade 必须显式执行标记语句"
        )
        assert re.search(r"op\.execute\(\s*drop_marked_constraint", text), (
            f"{path.name}: downgrade 必须显式执行来源判定语句"
        )


def test_contract_document_exists_and_covers_four_rules() -> None:
    doc = REPO / "docs" / "audits" / "2026-11-27-wp04-constraint-provenance-contract.md"
    assert doc.exists(), "兼容性契约必须落文档"
    text = doc.read_text(encoding="utf-8")
    for phrase in ("新建数据库", "既有数据库", "不一致", "状态差异"):
        assert phrase in text, f"契约缺少「{phrase}」条款"
