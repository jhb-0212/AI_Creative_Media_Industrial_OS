"""GenerationTask 状态机三副本一致性测试（契约 ↔ 代码 ↔ 迁移字面量）。

与 test_generation_route_parity 同纪律：状态机有三份副本——契约
（enum_registry / state_machines.yaml）、代码（GenerationTaskStatus /
GENERATION_TASK_ALLOWED_TRANSITIONS）、迁移播种字面量（b8c9d0e1f2a3 的
STATUS_VALUES / STATUS_TRANSITIONS）。迁移是历史快照不能 import 代码，
所以必须有测试把"代码改了、迁移没跟上"变成红的。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from services.generation_execution.domain.enums import GenerationTaskStatus
from services.generation_execution.domain.state_machine import (
    GENERATION_TASK_ALLOWED_TRANSITIONS,
)

_MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "data"
    / "migrations"
    / "versions"
    / "b8c9d0e1f2a3_create_generation_task_table.py"
)


def _load_migration() -> object:
    """按文件路径加载迁移模块（`versions/` 不是包，不能用 import 语句）。"""
    spec = importlib.util.spec_from_file_location("_generation_task_migration", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_status_values_match_enum() -> None:
    """迁移的 CHECK 取值集合必须与 GenerationTaskStatus 逐值相等。"""
    module = _load_migration()
    seeded = set(module.GENERATION_TASK_STATUS_VALUES)  # type: ignore[attr-defined]
    in_code = {member.value for member in GenerationTaskStatus}
    assert seeded == in_code, (
        f"迁移取值与枚举不一致：仅迁移有 {sorted(seeded - in_code)}，"
        f"仅代码有 {sorted(in_code - seeded)}"
    )


def test_migration_transitions_match_domain_table() -> None:
    """迁移播种的转移边必须与领域转移表逐边相等。"""
    module = _load_migration()
    seeded = set(module.GENERATION_TASK_STATUS_TRANSITIONS)  # type: ignore[attr-defined]
    in_code = {
        (src.value, tgt.value)
        for src, targets in GENERATION_TASK_ALLOWED_TRANSITIONS.items()
        for tgt in targets
    }
    assert seeded == in_code, (
        f"迁移转移边与领域转移表不一致：仅迁移有 {sorted(seeded - in_code)}，"
        f"仅代码有 {sorted(in_code - seeded)}"
    )


def test_migration_mutable_whitelist_covers_transition_metadata_only() -> None:
    """可变列白名单只含转移伴随元数据：准入事实列必须保持不可变。"""
    module = _load_migration()
    mutable = set(module._MUTABLE_COLUMNS)  # type: ignore[attr-defined]
    assert mutable == {"result_ref", "started_at", "completed_at"}
    # 准入事实不可事后改写（ACT-GEN-NEG-001 防抵赖面）。
    immutable_guard = {"attempt", "retry_of", "route_id", "budget_reservation_ref",
                       "quota_reservation_ref", "shot_spec_ref", "visual_plan_ref",
                       "constraint_snapshot_ref", "project_id", "shot_id"}
    assert mutable.isdisjoint(immutable_guard)
