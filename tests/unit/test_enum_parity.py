"""枚举与状态机的「契约 ↔ 代码 ↔ 迁移」三方一致性。

为什么需要这组测试，而不是只靠 `contract_check`：
`contract_check` 比对的是**契约与代码**。但状态机还有第三份副本——迁移里的
播种字面量（`data/migrations/versions/b3c4d5e6f7a8_*.py` 的 `STATUS_VALUES` /
`STATUS_TRANSITIONS`）。它必须独立存在（迁移是历史快照，不能 import 代码），
所以必须有测试把"代码改了、迁移没跟上"这类漂移变成红的。

三份副本的分工：
- 契约（`contracts/enums/enum_registry.yaml`、`state_machines.yaml`）：权威来源；
- 代码（`AssetStatus` / `ASSET_ALLOWED_TRANSITIONS`）：运行时判定；
- 迁移字面量：数据库里真正生效的约束（CHECK 取值 + 转移表播种）。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from scripts.contract_check import check_enum_parity
from services.asset_service.domain.enums import AssetStatus
from services.asset_service.domain.state_machine import ASSET_ALLOWED_TRANSITIONS
from services.shot_planning.domain.enums import (
    ConstraintPackStatus,
    ShotVisualPlanStatus,
)
from services.shot_planning.domain.state_machine import (
    CONSTRAINT_PACK_ALLOWED_TRANSITIONS,
    SHOT_VISUAL_PLAN_ALLOWED_TRANSITIONS,
)

_MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "data"
    / "migrations"
    / "versions"
    / "b3c4d5e6f7a8_asset_status_transitions.py"
)


def _load_migration() -> object:
    """按文件路径加载迁移模块（`versions/` 不是包，不能用 import 语句）。"""
    spec = importlib.util.spec_from_file_location("_asset_status_migration", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_status_values_match_enum() -> None:
    """迁移的 CHECK 取值集合必须与 `AssetStatus` 逐值相等。

    少一个：合法状态被数据库拒绝（业务写不进去）。
    多一个：数据库允许写入代码不认识的值（读出来即 ValueError → 500）。
    """
    module = _load_migration()
    seeded = set(module.STATUS_VALUES)  # type: ignore[attr-defined]
    in_code = {member.value for member in AssetStatus}
    assert seeded == in_code, (
        f"迁移取值与枚举不一致：仅迁移有 {sorted(seeded - in_code)}，"
        f"仅代码有 {sorted(in_code - seeded)}"
    )


def test_migration_transitions_match_state_machine() -> None:
    """迁移播种的边必须与 `ASSET_ALLOWED_TRANSITIONS` 完全一致。"""
    module = _load_migration()
    seeded = set(module.STATUS_TRANSITIONS)  # type: ignore[attr-defined]
    in_code = {
        (src.value, tgt.value)
        for src, targets in ASSET_ALLOWED_TRANSITIONS.items()
        for tgt in targets
    }
    assert seeded == in_code, (
        f"迁移边与域转移表不一致：仅迁移有 {sorted(seeded - in_code)}，"
        f"仅代码有 {sorted(in_code - seeded)}"
    )


# --------------------------------------------------------------------------- #
# ADR-R27 镜头域 1C：ConstraintPack / ShotVisualPlan 的「契约 ↔ 代码 ↔ 迁移」
# 三方一致性（迁移字面量在 d5e6f7a8b9c0）。
# --------------------------------------------------------------------------- #
_SHOT_MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "data"
    / "migrations"
    / "versions"
    / "d5e6f7a8b9c0_create_shot_planning_spec_pack_tables.py"
)


def _load_shot_migration() -> object:
    """按文件路径加载镜头域迁移模块（versions/ 不是包）。"""
    spec = importlib.util.spec_from_file_location("_shot_planning_migration_1c", _SHOT_MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_constraint_pack_migration_values_match_enum() -> None:
    """ConstraintPack 迁移 CHECK 取值必须与 `ConstraintPackStatus` 逐值相等。"""
    module = _load_shot_migration()
    seeded = set(module.CONSTRAINT_PACK_STATUS_VALUES)  # type: ignore[attr-defined]
    in_code = {member.value for member in ConstraintPackStatus}
    assert seeded == in_code, (
        f"迁移取值与枚举不一致：仅迁移有 {sorted(seeded - in_code)}，"
        f"仅代码有 {sorted(in_code - seeded)}"
    )


def test_constraint_pack_migration_transitions_match_state_machine() -> None:
    """ConstraintPack 迁移播种的边必须与领域转移表完全一致。"""
    module = _load_shot_migration()
    seeded = set(module.CONSTRAINT_PACK_STATUS_TRANSITIONS)  # type: ignore[attr-defined]
    in_code = {
        (src.value, tgt.value)
        for src, targets in CONSTRAINT_PACK_ALLOWED_TRANSITIONS.items()
        for tgt in targets
    }
    assert seeded == in_code, (
        f"迁移边与域转移表不一致：仅迁移有 {sorted(seeded - in_code)}，"
        f"仅代码有 {sorted(in_code - seeded)}"
    )


def test_visual_plan_migration_values_match_enum() -> None:
    """ShotVisualPlan 迁移 CHECK 取值必须与 `ShotVisualPlanStatus` 逐值相等。"""
    module = _load_shot_migration()
    seeded = set(module.SHOT_VISUAL_PLAN_STATUS_VALUES)  # type: ignore[attr-defined]
    in_code = {member.value for member in ShotVisualPlanStatus}
    assert seeded == in_code, (
        f"迁移取值与枚举不一致：仅迁移有 {sorted(seeded - in_code)}，"
        f"仅代码有 {sorted(in_code - seeded)}"
    )


def test_visual_plan_migration_transitions_match_state_machine() -> None:
    """ShotVisualPlan 迁移播种的边必须与领域转移表完全一致。"""
    module = _load_shot_migration()
    seeded = set(module.SHOT_VISUAL_PLAN_STATUS_TRANSITIONS)  # type: ignore[attr-defined]
    in_code = {
        (src.value, tgt.value)
        for src, targets in SHOT_VISUAL_PLAN_ALLOWED_TRANSITIONS.items()
        for tgt in targets
    }
    assert seeded == in_code, (
        f"迁移边与域转移表不一致：仅迁移有 {sorted(seeded - in_code)}，"
        f"仅代码有 {sorted(in_code - seeded)}"
    )


def test_state_machine_gate_passes_on_current_tree() -> None:
    """状态机闸门在当前代码树上必须通过（ConstraintPack 契约机读比对）。"""
    from scripts.contract_check import check_state_machines

    assert check_state_machines() is True


def test_enum_gate_passes_on_current_tree() -> None:
    """枚举闸门在当前代码树上必须通过（把 CI 闸门纳入 pytest 反馈回路）。

    这条不是重复：没有它，闸门坏了（比如读不到台账、正则失效）时 pytest 全绿，
    只有 CI 才会红；有了它，本地 `pytest tests` 就能发现闸门本身失效。
    """
    assert check_enum_parity() is True
