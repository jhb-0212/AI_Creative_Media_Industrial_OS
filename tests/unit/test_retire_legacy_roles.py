"""``scripts/retire_legacy_roles.py`` 的判定逻辑单测（不连库）。

为什么要给一个「只读检查脚本」写单测
------------------------------------
退役工具的价值全在判定上：``deletable`` 一旦判错，后果是两种事故之一。

- 判得太松（有引用却报「可删」）：``--drop`` 撞上 PostgreSQL 的依赖拒绝，
  工具输出与真实状态不符，人开始不信任它，转而「试删一次看看」——那正是
  这个脚本要替代的做法；
- 判得太严（真干净却报「有阻塞」）：角色永远退役不掉，登记文件里那条
  「待清库」注释就变成永久谎言。

两种都靠「取数 → 判定」分离来防：本文件只喂构造输入给 ``assess``，覆盖
「不存在」「存在且干净」「存在但有跨库影子依赖」「有可读引用但影子依赖为空」
四种组合。最后一条是**刻意**的：``pg_shdepend`` 是 ``DROP ROLE`` 的真实判据，
六类可读查询只是定位手段，二者矛盾时必须以影子依赖为准。
"""

from __future__ import annotations

from scripts import retire_legacy_roles as rlr


def _ref(kind: str = "table_acl", database: str = "ai_film_api_tests") -> rlr.Reference:
    return rlr.Reference(
        database=database,
        role="afs_audit",
        kind=kind,
        detail='{"object": "qc_governance.qc_tasks", "privilege": "SELECT"}',
    )


def test_absent_roles_are_not_deletable_and_not_blocked() -> None:
    """角色本来就没了：无需 DROP，也不该报阻塞（否则重跑会假红）。"""
    result = rlr.assess(existing=(), references=(), shadow_dependencies=())
    assert result.deletable is False
    assert result.shadow_dependencies == ()


def test_clean_roles_are_deletable() -> None:
    result = rlr.assess(
        existing=("afs_audit", "afs_data_repair"),
        references=(),
        shadow_dependencies=(),
    )
    assert result.deletable is True


def test_any_shadow_dependency_blocks_deletion() -> None:
    result = rlr.assess(
        existing=("afs_audit", "afs_data_repair"),
        references=(),
        shadow_dependencies=(("t5_lab", 86),),
    )
    assert result.deletable is False


def test_readable_reference_without_shadow_dependency_still_blocks() -> None:
    """可读查询命中、影子依赖没命中：保守判阻塞。

    这一格对应「列级授权」这类影子依赖查询容易漏、而可读查询能看见的情形。
    宁可多要人看一眼，也不要在信息不一致时放行 DROP。
    """
    result = rlr.assess(
        existing=("afs_audit",),
        references=(_ref(),),
        shadow_dependencies=(),
    )
    assert result.deletable is False


def test_report_lists_per_database_counts() -> None:
    """报告必须按库给数字：跨库依赖的处置是「一个库一个库清」，合计没意义。"""
    result = rlr.assess(
        existing=("afs_audit",),
        references=(_ref(database="lab_rework_tasks"),),
        shadow_dependencies=(("lab_rework_tasks", 95), ("ai_film_industrial", 95)),
    )
    report = rlr._report(result)
    assert report["deletable"] is False
    assert report["shadow_dependencies"] == [
        {"database": "lab_rework_tasks", "count": 95},
        {"database": "ai_film_industrial", "count": 95},
    ]
    assert report["references"][0]["database"] == "lab_rework_tasks"
