"""Project 领域模型单元测试（含状态机负向测试 + 幂等）。"""

from __future__ import annotations

import uuid

import pytest

from packages.errors.error_codes import DomainError
from services.project_planning.domain.enums import ProjectStatus, ProjectTier
from services.project_planning.domain.project import Project

_TENANT = uuid.UUID("00000000-0000-0000-0000-000000000000")


def _make_project() -> Project:
    return Project(
        entity_id="proj_001",
        project_name="测试项目",
        project_tier=ProjectTier.B,
        created_by="user_001",
        tenant_id=_TENANT,
    )


def test_project_defaults_to_draft() -> None:
    p = _make_project()
    assert p.status == ProjectStatus.DRAFT
    assert p.entity_type == "Project"
    assert p.owner_module == "project-planning"
    assert p.schema_version == 205
    assert p.entity_version == 1


def test_valid_transition_draft_to_active() -> None:
    p = _make_project()
    p.transition_to(ProjectStatus.ACTIVE, audit_ref="audit_001")
    assert p.status == ProjectStatus.ACTIVE
    assert p.entity_version == 2


def test_invalid_transition_is_rejected() -> None:
    """未登记转移一律拒绝（31.5 精神，负向优先）。"""
    p = _make_project()
    # DRAFT 不能直接跳 COMPLETED（未登记）
    with pytest.raises(DomainError, match="非法状态转移"):
        p.transition_to(ProjectStatus.COMPLETED, audit_ref="audit_002")


def test_terminal_state_cannot_be_revived() -> None:
    """终态不可原地复活（红线）。"""
    p = _make_project()
    p.transition_to(ProjectStatus.ACTIVE, audit_ref="a1")
    p.transition_to(ProjectStatus.COMPLETED, audit_ref="a2")
    p.transition_to(ProjectStatus.ARCHIVED, audit_ref="a3")
    assert p.status == ProjectStatus.ARCHIVED
    with pytest.raises(DomainError, match="非法状态转移"):
        p.transition_to(ProjectStatus.ACTIVE, audit_ref="a4")


def test_to_dict_returns_contract_envelope() -> None:
    p = _make_project()
    d = p.to_dict()
    assert d["entity_id"] == "proj_001"
    assert d["entity_type"] == "Project"
    assert d["status"] == "DRAFT"
    assert d["project_name"] == "测试项目"
    assert d["project_tier"] == "B"
    # 信封必需字段齐全
    for key in (
        "entity_id",
        "entity_type",
        "schema_version",
        "entity_version",
        "status",
        "owner_module",
        "created_at",
        "created_by",
        "updated_at",
        "audit_ref",
    ):
        assert key in d


# --------------------------------------------------------------------------- #
# organization_id 归属键（F04：租户隔离 ≠ 项目隔离）
# --------------------------------------------------------------------------- #
def test_project_organization_id_defaults_to_none() -> None:
    """未指定组织时 organization_id 为空（个人租户）。"""
    p = _make_project()
    assert p.organization_id is None


def test_project_carries_organization_id() -> None:
    """指定组织时 organization_id 正确承载（组织项目归属）。"""
    org_id = uuid.UUID("11111111-1111-1111-1111-111111111111")
    p = Project(
        entity_id="proj_002",
        project_name="组织项目",
        project_tier=ProjectTier.B,
        created_by="user_001",
        tenant_id=_TENANT,
        organization_id=org_id,
    )
    assert p.organization_id == org_id


def test_project_to_dict_does_not_leak_organization_id() -> None:
    """organization_id 是内部归属键，不进对外响应契约（与 tenant_id 一致）。

    组织归属应通过可信 Principal / 授权上下文获取，而非从业务响应里
    读一个可被客户端猜测的归属标识。
    """
    org_id = uuid.UUID("11111111-1111-1111-1111-111111111111")
    p = Project(
        entity_id="proj_003",
        project_name="归属项目",
        project_tier=ProjectTier.B,
        created_by="user_001",
        tenant_id=_TENANT,
        organization_id=org_id,
    )
    d = p.to_dict()
    assert "organization_id" not in d
    assert "tenant_id" not in d
