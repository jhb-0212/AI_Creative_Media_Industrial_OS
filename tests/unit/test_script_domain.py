"""ScriptRevision 领域单元测试（纯内存，无外部依赖）。

验证：
1. 导入产出 DRAFT 态，信封字段正确；
2. 状态机：DRAFT→LOCKED/SUPERSEDED 合法，LOCKED→SUPERSEDED 合法；
3. 未登记转移拒绝（31.5）；
4. 终态 SUPERSEDED 不可原地复活（红线）。
"""

from __future__ import annotations

import pytest

from services.project_planning.domain.enums import ScriptStatus
from services.project_planning.domain.script import ScriptRevision


def _make_script(entity_id: str = "scr_001") -> ScriptRevision:
    return ScriptRevision(
        entity_id=entity_id,
        project_id="proj_001",
        episode_ref="EP001",
        title="测试剧本",
        created_by="user_001",
    )


def test_import_produces_draft() -> None:
    s = _make_script()
    assert s.status == ScriptStatus.DRAFT
    assert s.entity_type == "ScriptRevision"
    assert s.schema_version == 205
    assert s.entity_version == 1


def test_draft_to_locked_legal() -> None:
    s = _make_script()
    s.transition_to(ScriptStatus.LOCKED, audit_ref="script.lock.approve")
    assert s.status == ScriptStatus.LOCKED
    assert s.entity_version == 2


def test_draft_to_superseded_legal() -> None:
    s = _make_script()
    s.transition_to(ScriptStatus.SUPERSEDED, audit_ref="superseded_by:scr_002")
    assert s.status == ScriptStatus.SUPERSEDED


def test_locked_to_superseded_legal() -> None:
    s = _make_script()
    s.transition_to(ScriptStatus.LOCKED, audit_ref="lock")
    s.transition_to(ScriptStatus.SUPERSEDED, audit_ref="superseded_by:scr_002")
    assert s.status == ScriptStatus.SUPERSEDED


def test_locked_cannot_revert_to_draft() -> None:
    s = _make_script()
    s.transition_to(ScriptStatus.LOCKED, audit_ref="lock")
    with pytest.raises(ValueError):
        s.transition_to(ScriptStatus.DRAFT, audit_ref="revert")


def test_superseded_is_terminal() -> None:
    s = _make_script()
    s.transition_to(ScriptStatus.SUPERSEDED, audit_ref="superseded_by:scr_002")
    with pytest.raises(ValueError):
        s.transition_to(ScriptStatus.DRAFT, audit_ref="revive")
    with pytest.raises(ValueError):
        s.transition_to(ScriptStatus.LOCKED, audit_ref="revive")


def test_to_dict_envelope() -> None:
    s = _make_script()
    d = s.to_dict()
    assert d["entity_id"] == "scr_001"
    assert d["status"] == "DRAFT"
    assert d["project_id"] == "proj_001"
    assert d["episode_ref"] == "EP001"
    assert d["title"] == "测试剧本"
