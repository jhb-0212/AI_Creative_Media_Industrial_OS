"""ProductionRoute 状态机三副本一致性测试（契约 ↔ 代码 ↔ 迁移字面量）。

为什么需要这组测试（与 test_enum_parity 同纪律）：
状态机有三份副本——契约（enum_registry / state_machines.yaml）、代码
（ProductionRouteStatus / PRODUCTION_ROUTE_ALLOWED_TRANSITIONS）、迁移播种
字面量（a5b6c7d8e9f0 的 STATUS_VALUES / STATUS_TRANSITIONS）。迁移是历史
快照不能 import 代码，所以必须有测试把"代码改了、迁移没跟上"变成红的。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from services.generation_execution.domain.enums import ProductionRouteStatus
from services.generation_execution.domain.state_machine import (
    PRODUCTION_ROUTE_ALLOWED_TRANSITIONS,
)

_MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "data"
    / "migrations"
    / "versions"
    / "a5b6c7d8e9f0_create_generation_execution_route_table.py"
)


def _load_migration() -> object:
    """按文件路径加载迁移模块（`versions/` 不是包，不能用 import 语句）。"""
    spec = importlib.util.spec_from_file_location("_generation_route_migration", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_status_values_match_enum() -> None:
    """迁移的 CHECK 取值集合必须与 ProductionRouteStatus 逐值相等。"""
    module = _load_migration()
    seeded = set(module.PRODUCTION_ROUTE_STATUS_VALUES)  # type: ignore[attr-defined]
    in_code = {member.value for member in ProductionRouteStatus}
    assert seeded == in_code, (
        f"迁移取值与枚举不一致：仅迁移有 {sorted(seeded - in_code)}，"
        f"仅代码有 {sorted(in_code - seeded)}"
    )


def test_migration_transitions_match_domain_table() -> None:
    """迁移播种的转移边必须与领域转移表逐边相等。"""
    module = _load_migration()
    seeded = set(module.PRODUCTION_ROUTE_STATUS_TRANSITIONS)  # type: ignore[attr-defined]
    in_code = {
        (src.value, tgt.value)
        for src, targets in PRODUCTION_ROUTE_ALLOWED_TRANSITIONS.items()
        for tgt in targets
    }
    assert seeded == in_code, (
        f"迁移转移边与领域转移表不一致：仅迁移有 {sorted(seeded - in_code)}，"
        f"仅代码有 {sorted(in_code - seeded)}"
    )
