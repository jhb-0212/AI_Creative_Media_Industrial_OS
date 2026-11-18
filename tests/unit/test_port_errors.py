"""仓储端口失败契约（P2-03）的单元测试。

这组用例守的不是"异常类存在"，而是**语义没有被压平**：
``IntegrityError`` 一个类型同时覆盖唯一冲突与外键冲突，服务层曾用同一分支处理，
于是"引用的项目刚被并发删掉"会被解释成"同一 id 重复提交，返回既有记录"。
把错误降级成成功，比报错更难发现。

因此核心用例是 ``test_foreign_key_violation_is_not_treated_as_duplicate``：
它断言的是"外键失败**不会**被翻译成可幂等的重复冲突"。
"""

from __future__ import annotations

import asyncio

import pytest

from packages.shared.port_errors import (
    DuplicateEntityError,
    ReferentialIntegrityError,
    RepositoryContractError,
    classify_integrity_error,
)
from packages.shared.versioning import ConcurrentModificationError
from services.project_planning.domain.project import Project
from services.project_planning.repository.in_memory_repo import InMemoryProjectRepository


class _FakeDriverError(Exception):
    """模拟驱动异常：携带 SQLSTATE（asyncpg 用 sqlstate，psycopg2 用 pgcode）。"""

    def __init__(self, sqlstate: str = "") -> None:
        super().__init__("driver error")
        if sqlstate:
            self.sqlstate = sqlstate


class _FakeSqlAlchemyError(Exception):
    """模拟 SQLAlchemy 包装：原始驱动异常挂在 ``orig`` 上。"""

    def __init__(self, orig: BaseException, message: str = "wrapped") -> None:
        super().__init__(message)
        self.orig = orig


# --------------------------------------------------------------------------- #
# 翻译：唯一冲突 vs 引用失败
# --------------------------------------------------------------------------- #
def test_unique_violation_becomes_duplicate_entity_error() -> None:
    """23505（唯一冲突）→ 可幂等恢复的 DuplicateEntityError。"""
    exc = _FakeSqlAlchemyError(_FakeDriverError("23505"))
    classified = classify_integrity_error(exc, entity_id="proj-1", constraint="pk_projects")
    assert isinstance(classified, DuplicateEntityError)
    assert classified.entity_id == "proj-1"
    assert classified.constraint == "pk_projects"
    assert classified.idempotent_recoverable is True
    assert "proj-1" in str(classified)


def test_foreign_key_violation_is_not_treated_as_duplicate() -> None:
    """23503（外键冲突）**不得**被当成重复提交——这是 P2-03 修复的语义缺陷。

    真实场景：创建集数时归属项目被并发删除。旧实现把 IntegrityError 一律当
    "重复提交"，回读到 None 后报 INT-500，或在有同名记录时错误地幂等返回。
    正确语义是"引用不成立，不可幂等"，必须上抛。
    """
    exc = _FakeSqlAlchemyError(_FakeDriverError("23503"))
    classified = classify_integrity_error(exc, entity_id="ep-1")
    assert isinstance(classified, ReferentialIntegrityError)
    assert not isinstance(classified, DuplicateEntityError)
    assert classified.idempotent_recoverable is False
    assert classified.sqlstate == "23503"


def test_check_violation_is_referential_not_duplicate() -> None:
    """23514（CHECK 违反）同样不可幂等：值不成立不是"已存在"。"""
    classified = classify_integrity_error(
        _FakeSqlAlchemyError(_FakeDriverError("23514")), entity_id="x"
    )
    assert isinstance(classified, ReferentialIntegrityError)


def test_pgcode_attribute_is_also_recognized() -> None:
    """psycopg2 风格（pgcode）也要识别：不能只认 asyncpg 的属性名。"""

    class _PgError(Exception):
        pgcode = "23505"

    classified = classify_integrity_error(_FakeSqlAlchemyError(_PgError()), entity_id="p")
    assert isinstance(classified, DuplicateEntityError)


def test_text_fallback_detects_duplicate_when_no_sqlstate() -> None:
    """取不到 SQLSTATE 时退回文本特征（驱动被中间层包装过的情形）。"""
    exc = _FakeSqlAlchemyError(
        Exception('duplicate key value violates unique constraint "pk_projects"')
    )
    assert isinstance(
        classify_integrity_error(exc, entity_id="p"), DuplicateEntityError
    )


def test_unknown_integrity_error_falls_back_to_referential() -> None:
    """两者都判不出时必须走"不可幂等"的保守分支。

    保守方向选错（把不认识的情况当重复提交）就等于把未知错误变成"成功返回"，
    这是最危险的一种失败：错误被吞掉，调用方看不出任何异常。
    """
    classified = classify_integrity_error(_FakeSqlAlchemyError(Exception("boom")), entity_id="p")
    assert isinstance(classified, ReferentialIntegrityError)


def test_both_errors_share_contract_base() -> None:
    """两个子类同一基类：端口契约可以被整体识别/整体告警。"""
    assert issubclass(DuplicateEntityError, RepositoryContractError)
    assert issubclass(ReferentialIntegrityError, RepositoryContractError)


# --------------------------------------------------------------------------- #
# 内存实现：可替换性证明（不再伪造 ORM 异常）
# --------------------------------------------------------------------------- #
def _project(entity_id: str = "p-1") -> Project:
    return Project(
        entity_id=entity_id,
        project_name="示例项目",
        created_by="user-a",
        tenant_id="t-1",
        organization_id=None,
    )


def test_in_memory_add_raises_port_contract_not_orm_error() -> None:
    """内存实现在主键冲突时抛端口契约异常（整改前它伪造 sqlalchemy.IntegrityError）。"""
    repo = InMemoryProjectRepository()
    asyncio.run(repo.add(_project()))
    with pytest.raises(DuplicateEntityError):
        asyncio.run(repo.add(_project()))


def test_in_memory_save_conflict_raises_concurrent_modification() -> None:
    """乐观锁失败抛 ConcurrentModificationError，与 SQL 适配器同语义。"""
    repo = InMemoryProjectRepository()
    project = _project()
    asyncio.run(repo.add(project))
    with pytest.raises(ConcurrentModificationError):
        asyncio.run(repo.save(project, expected_version=project.entity_version + 1))
