"""ADR 决策 2/3/5 的数据库不变量断言（防呆层）。

为什么需要：ADR 曾出现文档自相矛盾（决策 2 一边写「只存 episode_id」，一边
要求 `(project_id, episode_ref)` 复合外键），导致实现无论怎么做都「违反决策」。
本模块把 ADR 声明的结构不变量变成可执行断言：任何删除归属列、拆掉复合外键、
放松 outbox 租户约束的改动都会在此先红，迫使改动者显式修订 ADR，
而不是悄悄打补丁。

注意：这里只断言「结构不变量」，业务行为由 unit/integration 用例覆盖，不重复。
"""

from __future__ import annotations

from sqlalchemy import ForeignKeyConstraint

from packages.shared.outbox import OutboxEventModel
from services.project_planning.repository.orm import ScriptModel


def _composite_fk_to_episodes() -> list[ForeignKeyConstraint]:
    """返回 ScriptRevision 指向 episodes 的 (project_id, episode_ref) 复合外键。"""
    return [
        c
        for c in ScriptModel.__table__.constraints
        if isinstance(c, ForeignKeyConstraint)
        and {el.parent.name for el in c.elements} == {"project_id", "episode_ref"}
        and all(
            el.target_fullname.startswith("project_planning.episodes.")
            for el in c.elements
        )
    ]


def test_script_revision_keeps_composite_fk_ownership() -> None:
    """决策 2：归属列必须存在，且由复合外键指向 episodes。"""
    cols = {c.name for c in ScriptModel.__table__.columns}
    assert {"project_id", "episode_ref"} <= cols, "归属列缺失：复合外键将失去载体"
    assert _composite_fk_to_episodes(), (
        "缺少 (project_id, episode_ref) → episodes 复合外键，"
        "跨项目挂载将退回 Service 校验"
    )


def test_script_revision_is_tenant_scoped() -> None:
    """决策 5：核心业务表的 tenant_id 必须非空，否则 RLS 前提不成立。"""
    assert ScriptModel.__table__.c.tenant_id.nullable is False


def test_outbox_events_have_tenant_column_and_index() -> None:
    """决策 3/5：事件表 tenant_id 非空且建索引（列级约束）。

    RLS 策略「行为」由 tests/unit/test_outbox.py 的跨租户不可见用例覆盖；
    本用例只保证承载隔离所需的列与索引存在。
    """
    assert OutboxEventModel.__table__.c.tenant_id.nullable is False
    assert any(
        ix.name == "ix_outbox_tenant" for ix in OutboxEventModel.__table__.indexes
    ), "outbox 缺少 tenant_id 索引"
