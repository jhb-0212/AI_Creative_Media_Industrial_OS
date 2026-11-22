"""仓储 _to_domain 枚举防御的单元测试（L4：非法存量值降级为 DomainError）。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from packages.errors.error_codes import DomainError
from services.project_planning.repository.orm import ScriptModel
from services.project_planning.repository.script_sqlalchemy_repo import _to_domain


def _make_model(status: str) -> ScriptModel:
    """构造一个仅用于反序列化测试的 ORM 模型实例。"""
    now = datetime.now(UTC)
    return ScriptModel(
        entity_id="scr_001",
        entity_type="ScriptRevision",
        schema_version=205,
        entity_version=1,
        status=status,
        owner_module="project-planning",
        created_at=now,
        created_by="user_001",
        updated_at=now,
        audit_ref=None,
        project_id="proj_001",
        episode_ref="EP001",
        title="测试剧本",
    )


def test_valid_status_roundtrip() -> None:
    model = _make_model("DRAFT")
    domain = _to_domain(model)
    assert domain.status.value == "DRAFT"


def test_invalid_status_raises_domain_error() -> None:
    """存量非法状态值应降级为结构化 DomainError，而非裸 ValueError。"""
    model = _make_model("NOT_A_REAL_STATUS")
    with pytest.raises(DomainError) as exc_info:
        _to_domain(model)
    assert exc_info.value.error_code.code == "INTERNAL_ERROR"
    assert "NOT_A_REAL_STATUS" in str(exc_info.value.detail)
