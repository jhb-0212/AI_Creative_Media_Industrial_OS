"""归属范围判定的纯函数测试（AUTH-1 的资源输入那一半）。

这组用例不碰数据库：解析（IO）与判定（纯函数）在实现上是分开的，判定逻辑
必须能在毫秒级被枚举验证。数据库无法覆盖的组合（跨租户、缺组织、缺资源）
在这里穷举；真实端点行为由 tests/unit/test_resource_scope_api.py 证明。
"""

from __future__ import annotations

import uuid

import pytest

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.identity.ownership import (
    OwnershipPath,
    ResourceRef,
    assert_ownership_scope,
)
from packages.identity.principal import Principal

_TENANT = uuid.UUID("00000000-0000-0000-0000-000000000000")
_ORG_A = uuid.uuid4()
_ORG_B = uuid.uuid4()


def _principal(organization_id: uuid.UUID | None) -> Principal:
    return Principal(subject_id="u1", tenant_id=_TENANT, organization_id=organization_id)


def _resource() -> ResourceRef:
    return ResourceRef("AssetVersion", "asset-1")


def test_missing_ownership_is_not_found() -> None:
    """解析不到归属 → 404（不可见与不存在同形，避免存在性探测）。"""
    with pytest.raises(DomainError) as exc:
        assert_ownership_scope(_principal(_ORG_A), _resource(), None)
    assert exc.value.error_code is ErrorRegistry.NOT_FOUND


def test_cross_tenant_ownership_is_not_found() -> None:
    """归属租户与主体不一致 → 404（解析结果不可信时按不可见处理）。"""
    other_tenant = uuid.uuid4()
    with pytest.raises(DomainError) as exc:
        assert_ownership_scope(
            _principal(_ORG_A),
            _resource(),
            OwnershipPath(tenant_id=other_tenant, organization_id=_ORG_A),
        )
    assert exc.value.error_code is ErrorRegistry.NOT_FOUND


def test_personal_project_is_reachable_within_tenant() -> None:
    """个人项目（无组织）→ 租户内即达，不做组织限定。"""
    assert_ownership_scope(
        _principal(None),
        _resource(),
        OwnershipPath(tenant_id=_TENANT, organization_id=None),
    )


def test_same_organization_is_allowed() -> None:
    """同组织成员 → 放行。"""
    assert_ownership_scope(
        _principal(_ORG_A),
        _resource(),
        OwnershipPath(tenant_id=_TENANT, organization_id=_ORG_A, project_id="p1"),
    )


def test_cross_organization_is_denied() -> None:
    """同租户跨组织 → 403（看得见但无权碰，报 404 会掩盖真实原因）。"""
    with pytest.raises(DomainError) as exc:
        assert_ownership_scope(
            _principal(_ORG_B),
            _resource(),
            OwnershipPath(tenant_id=_TENANT, organization_id=_ORG_A, project_id="p1"),
        )
    assert exc.value.error_code is ErrorRegistry.AUTH_SCOPE_DENIED


def test_personal_principal_cannot_touch_organization_resource() -> None:
    """无 org claim 的主体不得访问组织资源（fail-closed）。"""
    with pytest.raises(DomainError) as exc:
        assert_ownership_scope(
            _principal(None),
            _resource(),
            OwnershipPath(tenant_id=_TENANT, organization_id=_ORG_A, project_id="p1"),
        )
    assert exc.value.error_code is ErrorRegistry.AUTH_SCOPE_DENIED
