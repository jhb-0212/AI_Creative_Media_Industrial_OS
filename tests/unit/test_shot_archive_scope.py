"""「哪些镜头状态算在制」必须是契约状态集的一个**显式划分**。

要防的那件事
------------
批次六的双向守卫把阻断集合在四处各写了一次（两个守卫函数 + 两个触发器的
WHEN 子句）。这类副本的失效方式不是报错，而是**静默**：新增一个算在制的状态、
改了守卫、忘了 WHEN，触发器就不再触发，而所有用旧状态写的测试照样全绿。
本项目已经在 `qc_reports` / `episodes` / `OWNERSHIP_STACK` 上三次栽在同一形态。

所以这里测的不是「守卫逻辑对不对」（那是 `verify_batch7.py` 在真库上做的），
而是「集合是否只有一处、且是否覆盖契约全部状态」。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[2]
_MIGRATION = (
    _ROOT
    / "data"
    / "migrations"
    / "pending"
    / "feat-ownership"
    / "k1f2a3b4c5d6_shot_archive_blocking_states.py"
)
_BLOCKING_FN = "shot_planning.shot_archive_blocking_states"


def _load_migration():
    """按路径加载迁移模块（与 `lab._load` 同法：先登记再 exec）。

    只读它的常量与 SQL 生成函数——那些正是本批次的被测命题；执行迁移本身
    由 `verify_batch7.py` 在实验库上负责。
    """
    spec = importlib.util.spec_from_file_location("mig_batch7", _MIGRATION)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _unpartitioned(
    blocking: set[str], non_blocking: set[str], contract: set[str]
) -> tuple[set[str], set[str]]:
    """返回 (未归类状态, 相撞状态)。

    独立成函数是为了能被**反向喂入**：一个只会在正确数据上返回空集的实现
    （例如 `return set(), set()`）无法通过下面那条变异用例。
    """
    return contract - (blocking | non_blocking), blocking & non_blocking


@pytest.fixture(scope="module")
def migration():
    return _load_migration()


@pytest.fixture(scope="module")
def shot_states() -> set[str]:
    data = yaml.safe_load(
        (_ROOT / "contracts" / "state_machines" / "state_machines.yaml").read_text(
            encoding="utf-8"
        )
    )
    entry = next(o for o in data["objects"] if o.get("object") == "Shot")
    return set(entry["allowed_states"])


def test_blocking_and_non_blocking_partition_contract_states(
    migration, shot_states: set[str]
) -> None:
    """划分必须完备且不交：新增镜头状态时这条会红，逼出一次显式裁定。

    为什么不允许「默认不算在制」：默认放行会静默得到「归档不拦 + 归档后不可
    复活」的中间地带——资源可能正在投入，而数据已经封存，没有任何报错。
    """
    missing, overlap = _unpartitioned(
        set(migration.BLOCKING_STATES),
        set(migration.NON_BLOCKING_STATES),
        shot_states,
    )
    assert overlap == set(), f"同一状态既算在制又不算在制：{sorted(overlap)}"
    assert missing == set(), f"阻断集合未分类这些契约状态：{sorted(missing)}"


def test_blocked_is_declared_non_blocking(migration) -> None:
    """`BLOCKED` 不阻断归档（2026-11-21 裁定），且理由是可验证的。

    它不阻断的**前提**是 B 侧守卫关死了复活路径：归档后的 BLOCKED 镜头永远
    推不进 IN_PRODUCTION。这个前提由 ``verify_batch7.py`` 的 S3 在真库上验证；
    本用例只钉住「集合里的分类没有被悄悄改掉」。
    """
    assert "BLOCKED" in migration.NON_BLOCKING_STATES
    assert "BLOCKED" not in migration.BLOCKING_STATES
    assert "IN_PRODUCTION" in migration.BLOCKING_STATES


def test_status_literal_exists_only_in_the_blocking_function(migration) -> None:
    """upgrade 的 SQL 里，状态字面量只允许出现在阻断集合函数中。

    这是「单源」的可机检表达：如果守卫或触发器里又出现 `= 'IN_PRODUCTION'`，
    那么下一次改集合就只有函数会变——正是本批次要削掉的那个副本。
    """
    steps = [step.sql for step in migration.upgrade_steps()]
    definitions = [sql for sql in steps if f"FUNCTION {_BLOCKING_FN}" in sql]

    assert len(definitions) == 1, "upgrade 里必须恰好有一处定义阻断集合函数"
    for sql in (sql for sql in steps if sql not in definitions):
        assert "'IN_PRODUCTION'" not in sql, (
            "状态字面量出现在阻断集合函数之外：改集合会变成改多处：\n" + sql
        )

    referencing = [sql for sql in steps if f"{_BLOCKING_FN}()" in sql]
    assert len(referencing) == 3, (
        "应当只有「函数定义 + 两个守卫」引用该函数，实际 "
        f"{len(referencing)} 处（触发器若也引用它，说明 WHEN 又拿它做过滤了）"
    )


def test_triggers_do_not_prefilter_by_status(migration) -> None:
    """触发器的 WHEN 子句不得按状态过滤，否则函数里的判据永远不会执行。"""
    trigger_sqls = [
        step.sql
        for step in migration.upgrade_steps()
        if step.sql.lstrip().upper().startswith("CREATE TRIGGER")
    ]
    assert len(trigger_sqls) == 2, "本批次应当重建两个触发器"
    for sql in trigger_sqls:
        assert " WHEN " not in sql.upper(), (
            "触发器仍带 WHEN 预过滤：新增阻断状态时它会静默不触发：\n" + sql
        )


def test_downgrade_restores_guards_before_dropping_function(migration) -> None:
    """回退顺序不可反：先还原守卫，最后删函数。

    plpgsql 是延迟解析——守卫引用了不存在的函数不会在 CREATE 时报错，而是在
    **触发时**报 42883。顺序反了不会有任何即时反馈，只会在某次归档时突然失败。
    所以顺序必须被断言，而不是靠注释提醒。
    """
    steps = [step.sql for step in migration.downgrade_steps()]
    drop_index = next(
        i for i, sql in enumerate(steps) if "DROP FUNCTION" in sql.upper()
    )
    assert drop_index == len(steps) - 1, "删函数必须是回退的最后一步"
    restored = " ".join(steps[:drop_index])
    assert "scenes_archive_guard" in restored
    assert "shots_parent_scene_guard" in restored
    # 回退后的守卫必须回到批次六的写法（字面量），否则「回退」只是换了个实现。
    assert "'IN_PRODUCTION'" in restored


def test_partition_check_detects_unclassified_state(
    migration, shot_states: set[str]
) -> None:
    """变异反证：新增一个契约状态时，划分检查必须报出它。

    不做这一步，「划分完备」只是当前数据下为真的陈述；写成
    ``assert set() == set()`` 之类的恒真式也照样绿。
    """
    blocking = set(migration.BLOCKING_STATES)
    non_blocking = set(migration.NON_BLOCKING_STATES)

    assert _unpartitioned(blocking, non_blocking, shot_states) == (set(), set())
    assert _unpartitioned(blocking, non_blocking, shot_states | {"REWORK"}) == (
        {"REWORK"},
        set(),
    )
