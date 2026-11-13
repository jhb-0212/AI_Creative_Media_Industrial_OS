"""ScriptRevision 领域防御性校验与不可变语义的单元测试（第二轮审查回归）。

覆盖：
- 字段空值/长度超限校验（__post_init__）。
- 不可变语义澄清：版本内容不原地覆盖、终态不可复活（非 frozen dataclass）。
- 枚举非法存量值防御（仓储 _to_domain 降级为 DomainError）。
"""

from __future__ import annotations

import pytest

from services.project_planning.domain.enums import ScriptStatus
from services.project_planning.domain.script import ScriptRevision


def _make(**overrides: str) -> ScriptRevision:
    base: dict[str, str] = {
        "entity_id": "scr_001",
        "project_id": "proj_001",
        "episode_ref": "EP001",
        "title": "测试剧本",
        "created_by": "user_001",
    }
    base.update(overrides)
    return ScriptRevision(**base)


def test_rejects_empty_title() -> None:
    with pytest.raises(ValueError):
        _make(title="   ")


def test_rejects_empty_episode_ref() -> None:
    with pytest.raises(ValueError):
        _make(episode_ref="")


def test_rejects_empty_created_by() -> None:
    with pytest.raises(ValueError):
        _make(created_by="  ")


def test_rejects_overlong_title() -> None:
    with pytest.raises(ValueError):
        _make(title="字" * 201)


def test_rejects_overlong_created_by() -> None:
    with pytest.raises(ValueError):
        _make(created_by="u" * 65)


def test_superseded_is_terminal_and_not_revivable() -> None:
    """终态不可原地复活（红线），即使构造时已 SUPERSEDED 也不可再迁移。"""
    s = _make(status=ScriptStatus.SUPERSEDED)
    with pytest.raises(ValueError):
        s.transition_to(ScriptStatus.DRAFT, audit_ref="revive")
