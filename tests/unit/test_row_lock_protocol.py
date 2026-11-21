"""父行锁协议：声明侧（边表）与执行侧（SQL）的一致性。

为什么需要一个专门的用例文件
------------------------------------------------------------
SC-5 有两条腿，缺任一条都不成立：

* **声明**——哪些边需要与父/子对象的并发写入串行化；
* **执行**——那两条语句真的取了互斥的锁。

两条腿都能「看起来写了」而实际无效：声明漏了三条进入在产的边中的一条，不变量
从另一条边被绕过；锁模式写成 ``FOR NO KEY UPDATE``（SQLAlchemy 只给
``key_share=True`` 时的渲染结果），与自身不冲突，屏障一次都没挡住，而代码读起来
完全正确。所以这里既验「集合是否完备」，也验「渲染出的锁子句逐字是什么」。

集合一律**派生**，不手写：``[e for e in edges if e.target is IN_PRODUCTION]``。
手写清单的陈旧方向恰好是不安全的一侧——新增一条边就是漏检，而漏检表现为绿灯。
"""

from __future__ import annotations

import pytest
from sqlalchemy.dialects import postgresql

from packages.shared.lifecycle import (
    ParentPrecondition,
    Transition,
    validate_lifecycle,
)
from services.project_planning.domain.enums import SceneStatus, ShotStatus
from services.project_planning.domain.state_machine import (
    SCENE_ARCHIVE_CHILD_PRECONDITION,
    SCENE_LIFECYCLE,
    SHOT_IN_PRODUCTION_PARENT_PRECONDITION,
    SHOT_LIFECYCLE,
)
from services.project_planning.repository.shot_planning_sqlalchemy_repos import (
    scene_lock_statement,
)


# --------------------------------------------------------------------------- #
# 执行侧：渲染出的锁子句
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("exclusive", "expected_clause"),
    [
        (True, "FOR UPDATE"),
        (False, "FOR SHARE"),
    ],
)
def test_lock_statement_renders_the_agreed_clause(
    exclusive: bool, expected_clause: str
) -> None:
    """两侧锁模式逐字钉住。

    子对象侧必须是 ``FOR SHARE`` 而非 ``FOR KEY SHARE``：后者与 ``FOR NO KEY
    UPDATE`` 兼容，而 ``UPDATE ... SET status`` 取的正是 ``NO KEY UPDATE``——
    只靠它会漏掉「父侧没显式加排他锁的归档」。方言升级、参数写漏、或有人
    「简化」这个调用，都必须在这里变红，而不是在生产里表现为「偶发的归档失效」。
    """
    sql = str(scene_lock_statement("scene-1", exclusive=exclusive).compile(
        dialect=postgresql.dialect()
    ))
    assert sql.rstrip().endswith(expected_clause), sql
    assert "FOR NO KEY UPDATE" not in sql
    if not exclusive:
        assert "FOR KEY SHARE" not in sql, (
            "FOR KEY SHARE 与 NO KEY UPDATE 兼容，挡不住父行的普通状态更新"
        )


# --------------------------------------------------------------------------- #
# 声明侧：边的完备性与两侧配对
# --------------------------------------------------------------------------- #
def test_every_edge_into_production_declares_the_parent_barrier() -> None:
    """所有进入在产的边都必须带父场次屏障（集合派生，不是手写清单）。"""
    edges = [e for e in SHOT_LIFECYCLE.edges if e.target is ShotStatus.IN_PRODUCTION]
    assert edges, "Shot 没有任何进入在产的边：状态机结构已变，本判据失效"
    missing = [e.label for e in edges if e.parent_precondition is None]
    assert not missing, (
        f"这些进入在产的边没有声明父场次屏障：{missing}。"
        "漏一条，不变量就能从那条边被绕过"
    )


def test_every_edge_into_archive_declares_the_child_barrier() -> None:
    """所有进入归档的边都必须带子对象屏障（同理派生）。"""
    edges = [e for e in SCENE_LIFECYCLE.edges if e.target is SceneStatus.ARCHIVED]
    assert edges, "Scene 没有进入归档的边：状态机结构已变，本判据失效"
    missing = [e.label for e in edges if e.child_precondition is None]
    assert not missing, f"这些进入归档的边没有声明子对象屏障：{missing}"


def test_the_two_sides_declare_the_same_pair() -> None:
    """家长与孩子必须指向同一对（对象, 状态），否则两条屏障各挡各的。"""
    assert SHOT_IN_PRODUCTION_PARENT_PRECONDITION.object_name == "Scene"
    assert (
        SHOT_IN_PRODUCTION_PARENT_PRECONDITION.forbidden_status
        == SceneStatus.ARCHIVED.value
    )
    assert SCENE_ARCHIVE_CHILD_PRECONDITION.object_name == "Shot"
    assert (
        SCENE_ARCHIVE_CHILD_PRECONDITION.forbidden_status
        == ShotStatus.IN_PRODUCTION.value
    )
    # 说明文本要能独立成句地解释「为什么被拒」，它会被原样放进错误详情。
    assert SHOT_IN_PRODUCTION_PARENT_PRECONDITION.message.strip()
    assert SCENE_ARCHIVE_CHILD_PRECONDITION.message.strip()


def test_precondition_with_empty_field_is_rejected_at_construction() -> None:
    """空声明的失败必须发生在 import 期，而不是运行期去锁一张不存在的表。"""
    problems = validate_lifecycle(
        "Probe",
        [SceneStatus.DRAFT, SceneStatus.ACTIVE],
        [
            Transition(
                SceneStatus.DRAFT,
                SceneStatus.ACTIVE,
                "PROBE.CHANGE",
                parent_precondition=ParentPrecondition(
                    object_name="", forbidden_status="", message=""
                ),
            )
        ],
    )

    joined = "；".join(problems)
    assert "parent_precondition.object_name 为空" in joined
    assert "parent_precondition.forbidden_status 为空" in joined
    assert "parent_precondition.message 为空" in joined
