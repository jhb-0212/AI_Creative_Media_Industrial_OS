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

from packages.errors.error_codes import DomainError
from services.project_planning.domain.enums import ScriptStatus
from services.project_planning.domain.script import ScriptRevision
from services.project_planning.domain.state_machine import (
    SCRIPT_IMPORT_ACTION,
    SCRIPT_LOCK_APPROVE_ACTION,
    SCRIPT_LOCK_REQUEST_ACTION,
    SCRIPT_TRANSITIONS,
)

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
    s.request_lock("writer_001", "tr:req", SCRIPT_LOCK_REQUEST_ACTION)
    assert s.status == ScriptStatus.LOCK_PENDING
    assert s.requested_by == "writer_001"
    assert s.entity_version == 2

    s.approve_lock("director_001", "tr:app", SCRIPT_LOCK_APPROVE_ACTION)
    assert s.status == ScriptStatus.LOCKED
    assert s.entity_version == 3


def test_draft_cannot_approve_directly() -> None:
    """DRAFT 不能直接 approve（须先申请，进入 LOCK_PENDING）。"""
    s = _make_script()
    with pytest.raises(DomainError):
        s.approve_lock("director_001", "tr:app", SCRIPT_LOCK_APPROVE_ACTION)


def test_draft_to_superseded_legal() -> None:
    s = _make_script()
    s.transition_to(
        ScriptStatus.SUPERSEDED,
        audit_ref="superseded_by:scr_002",
        action=SCRIPT_IMPORT_ACTION,
    )
    assert s.status == ScriptStatus.SUPERSEDED


@pytest.mark.parametrize(
    ("source", "target"),
    [(edge.source, edge.target) for edge in SCRIPT_TRANSITIONS],
    ids=[edge.label for edge in SCRIPT_TRANSITIONS],
)
def test_edge_ownership_is_enforced_by_aggregate(
    source: ScriptStatus, target: ScriptStatus
) -> None:
    """五条边分属三个动作，互不越界（N-07 在本域的关键后果）。

    这里用一个**确实拥有其他边**的动作去请求本边：例如拿
    ``SCRIPT.IMPORT``（拥有三条取代边）推进 ``LOCK_PENDING→LOCKED``。
    整改前这条路是通的——「导入新版本」可以顺手把一个待批剧本变成已锁定，
    而审计表会记下 ``owner_action=SCRIPT.IMPORT``，看起来完全合法。

    所有权判定把这个可能性封在结构层：不是多一个 if 拦住，而是那条边不在
    该动作的可达集里。
    """
    script = ScriptRevision(
        entity_id="scr_edge",
        project_id="proj_001",
        episode_ref="EP001",
        title="边所有权",
        created_by="user_001",
        tenant_id=_TENANT,
        status=source,
    )
    owner = next(e.owner_action for e in SCRIPT_TRANSITIONS if e.key == (source, target))
    foreign = next(
        action
        for action in (
            SCRIPT_IMPORT_ACTION,
            SCRIPT_LOCK_REQUEST_ACTION,
            SCRIPT_LOCK_APPROVE_ACTION,
        )
        if action != owner
    )

    with pytest.raises(DomainError, match="独占"):
        script.transition_to(target, "ref", foreign)
    assert script.status is source
    assert script.entity_version == 1


def test_locked_to_superseded_legal() -> None:
    s = _make_script()
    s.request_lock("writer_001", "tr:req", SCRIPT_LOCK_REQUEST_ACTION)
    s.approve_lock("director_001", "tr:app", SCRIPT_LOCK_APPROVE_ACTION)
    s.transition_to(
        ScriptStatus.SUPERSEDED,
        audit_ref="superseded_by:scr_002",
        action=SCRIPT_IMPORT_ACTION,
    )
    assert s.status == ScriptStatus.SUPERSEDED


def test_locked_cannot_revert_to_draft() -> None:
    s = _make_script()
    s.request_lock("writer_001", "tr:req", SCRIPT_LOCK_REQUEST_ACTION)
    s.approve_lock("director_001", "tr:app", SCRIPT_LOCK_APPROVE_ACTION)
    with pytest.raises(DomainError):
        s.transition_to(
            ScriptStatus.DRAFT, audit_ref="revert", action=SCRIPT_LOCK_APPROVE_ACTION
        )


def test_superseded_is_terminal() -> None:
    s = _make_script()
    s.transition_to(
        ScriptStatus.SUPERSEDED,
        audit_ref="superseded_by:scr_002",
        action=SCRIPT_IMPORT_ACTION,
    )
    with pytest.raises(DomainError):
        s.transition_to(
            ScriptStatus.DRAFT, audit_ref="revive", action=SCRIPT_IMPORT_ACTION
        )
    with pytest.raises(DomainError):
        s.transition_to(
            ScriptStatus.LOCKED, audit_ref="revive", action=SCRIPT_IMPORT_ACTION
        )


def test_to_dict_envelope() -> None:
    s = _make_script()
    d = s.to_dict()
    assert d["entity_id"] == "scr_001"
    assert d["status"] == "DRAFT"
    assert d["project_id"] == "proj_001"
    assert d["episode_ref"] == "EP001"
    assert d["title"] == "测试剧本"
    # tenant_id 是内部隔离键，不进对外序列化（契约未声明该字段）。
    assert "tenant_id" not in d
    assert d["requested_by"] is None

def test_idempotency_key_reuse_with_different_content_conflicts() -> None:
    """P3-1：同 entity_id 不同内容必须 409，不能返回旧版本冒充成功。"""
    from packages.errors.error_codes import DomainError, ErrorRegistry
    from services.project_planning.service.script_service import _assert_same_request

    s = _make_script()
    _assert_same_request(
        s, project_id="proj_001", episode_ref="EP001", title="测试剧本"
    )
    with pytest.raises(DomainError) as exc:
        _assert_same_request(
            s, project_id="proj_001", episode_ref="EP001", title="另一个标题"
        )
    assert exc.value.error_code is ErrorRegistry.IDEMPOTENCY_CONFLICT
