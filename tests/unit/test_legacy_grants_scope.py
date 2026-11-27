"""`legacy_statements` 的冻结范围：降级只回滚本迁移真正授过的表。

真实失败：`head → 7a8b9c0d1e2f` 在 `c1a2b3d4e5f6` 处死于
``relation "project_planning.scenes" does not exist``——该迁移 upgrade 时只授了
10 张表，降级却按今天的矩阵去 REVOKE ``scenes``（更晚的 `a13b0c1d2e3f` 建的表，
在该降级点已被删掉）。
"""

from __future__ import annotations

from packages.shared.db_grants import DOMAIN_GRANTS, DOMAINS, legacy_statements
from packages.shared.db_roles import domain_role

_PROJECT = "project_planning"


def test_unbounded_legacy_statements_cover_whole_matrix() -> None:
    full = legacy_statements(domain_role(_PROJECT), _PROJECT)
    assert any(f"{_PROJECT}.episodes" in sql for sql in full)


def test_frozen_scope_only_touches_declared_tables() -> None:
    full = legacy_statements(domain_role(_PROJECT), _PROJECT)
    frozen = legacy_statements(domain_role(_PROJECT), _PROJECT, tables={"episodes"})
    assert len(frozen) < len(full), "冻结范围没有生效：仍然生成了全量语句"
    assert any(f"{_PROJECT}.episodes" in sql for sql in frozen)
    assert not any(f"{_PROJECT}.projects" in sql for sql in frozen)


def test_frozen_scope_excludes_tables_from_later_revisions() -> None:
    """`scenes` 属于更晚的迁移；本迁移的冻结范围里没有它，就不得出现。"""
    matrix_tables = {grant.table for grant in DOMAIN_GRANTS[_PROJECT]}
    if "scenes" not in matrix_tables:
        return
    frozen = legacy_statements(
        domain_role(_PROJECT), _PROJECT, tables=frozenset({"projects", "episodes"})
    )
    assert not any("scenes" in sql for sql in frozen)


def test_all_domains_support_frozen_scope() -> None:
    for domain in DOMAINS:
        statements = legacy_statements(domain_role(domain), domain, tables=frozenset())
        assert statements, f"{domain} 冻结为空表时仍应生成默认权限回滚语句"
