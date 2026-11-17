"""ShotRouteSelection 状态取值三副本一致性（契约 ↔ 代码 ↔ 迁移字面量）。

与 test_generation_route_parity 同纪律：迁移是历史快照，不能 import 代码，
所以必须由测试把"代码改了、迁移没跟上"变成红的。

本对象还有一条额外的结构性要求：**没有转移表**。契约登记的是单值状态集
（ACTIVE，无转移边）；如果哪天代码里冒出一条转移边，说明有人在不改契约的
前提下给"生效路线"加生命周期——那正是本对象要防的事（改写即绕过单一路线）。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from services.generation_execution.domain import state_machine
from services.generation_execution.domain.enums import ShotRouteSelectionStatus

_MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "data"
    / "migrations"
    / "versions"
    / "e1f2a3b4c5d6_create_shot_route_selection.py"
)


def _load_migration() -> object:
    """按文件路径加载迁移模块（`versions/` 不是包，不能用 import 语句）。"""
    spec = importlib.util.spec_from_file_location(
        "_shot_route_selection_migration", _MIGRATION
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_status_values_match_enum() -> None:
    """迁移的 CHECK 取值集合必须与枚举逐值相等（当前均为单值 ACTIVE）。"""
    module = _load_migration()
    seeded = set(module.SHOT_ROUTE_SELECTION_STATUS_VALUES)  # type: ignore[attr-defined]
    in_code = {member.value for member in ShotRouteSelectionStatus}
    assert seeded == in_code, (
        f"迁移取值与枚举不一致：仅迁移有 {sorted(seeded - in_code)}，"
        f"仅代码有 {sorted(in_code - seeded)}"
    )


def test_unique_constraint_name_is_stable() -> None:
    """唯一约束名是迁移与 ORM 的共同契约（alembic check 靠它判定一致）。"""
    module = _load_migration()
    assert module.UNIQUE_NAME == "ux_shot_route_selection_effective"  # type: ignore[attr-defined]


def test_no_transition_table_in_domain() -> None:
    """领域层不得为该对象定义转移线：单值状态集没有"下一步"。"""
    suspicious = [
        name
        for name in dir(state_machine)
        if name.startswith("SHOT_ROUTE_SELECTION") and "TRANSITION" in name
    ]
    assert suspicious == [], (
        f"ShotRouteSelection 出现转移定义 {suspicious}——契约是单值状态集，"
        "要加生命周期必须先走契约修订（V205-R14-A9）"
    )
