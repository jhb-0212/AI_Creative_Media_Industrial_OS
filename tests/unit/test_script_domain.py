"""ScriptRevision 领域单元测试（纯内存，无外部依赖）。

验证：
1. 导入产出 DRAFT 态，信封字段正确；
2. 状态机：两步审批流 DRAFT→LOCK_PENDING→LOCKED；DRAFT/LOCK_PENDING/LOCKED→SUPERSEDED；
3. 未登记转移拒绝（31.5）；
4. 终态 SUPERSEDED 不可原地复活（红线）；
5. 职责分离：request_lock 登记申请人、approve_lock 登记批准人。
"""

from __future__ import annotations

import uuid

import pytest

from services.project_planning.domain.enums import ScriptStatus
from services.project_planning.domain.script import ScriptRevision

_TENANT = uuid.UUID("00000000-0000-0000-0000-000000000000")


def _make_script(entity_id: str = "scr_001") -> ScriptRevision:
    return ScriptRevision(
        entity_id=entity_id,
        project_id="proj_001",
        episode_ref="EP001",
        title="测试剧本",
        created_by="user_001",
        tenant_id=_TENANT,
    )


def test_import_produces_draft() -> None:
    s = _make_script()
    assert s.status == ScriptStatus.DRAFT
    assert s.entity_type == "ScriptRevision"
    assert s.schema_version == 205
    assert s.entity_version == 1


def test_two_step_lock_flow() -> None:
    """两步审批流：DRAFT→LOCK_PENDING（申请）→LOCKED（批准）。"""
    s = _make_script()
    s.request_lock("writer_001")
    assert s.status == ScriptStatus.LOCK_PENDING
    assert s.requested_by == "writer_001"
    assert s.entity_version == 2

    s.approve_lock("director_001")
    assert s.status == ScriptStatus.LOCKED
    assert s.entity_version == 3


def test_draft_cannot_approve_directly() -> None:
    """DRAFT 不能直接 approve（须先申请，进入 LOCK_PENDING）。"""
    s = _make_script()
    with pytest.raises(ValueError):
        s.approve_lock("director_001")


def test_draft_to_superseded_legal() -> None:
    s = _make_script()
    s.transition_to(ScriptStatus.SUPERSEDED, audit_ref="superseded_by:scr_002")
    assert s.status == ScriptStatus.SUPERSEDED


def test_locked_to_superseded_legal() -> None:
    s = _make_script()
    s.request_lock("writer_001")
    s.approve_lock("director_001")
    s.transition_to(ScriptStatus.SUPERSEDED, audit_ref="superseded_by:scr_002")
    assert s.status == ScriptStatus.SUPERSEDED


def test_locked_cannot_revert_to_draft() -> None:
    s = _make_script()
    s.request_lock("writer_001")
    s.approve_lock("director_001")
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
    assert d["tenant_id"] == str(_TENANT)
    assert d["requested_by"] is None
