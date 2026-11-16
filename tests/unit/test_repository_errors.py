"""仓储异常语义的单元测试（审查第 16 条）。

要证明的不是"翻译函数能跑"，而是三个语义边界：

1. **两种冲突不再共用同一个异常**：唯一冲突 → ``DuplicateEntityError``
   （可幂等返回），乐观锁冲突 → ``ConcurrentModificationError``（必须重试）。
   把前者当后者（或反过来）都是**处置反了**，不是措辞问题。
2. **未识别的完整性冲突不会被吞**：落到内部错误，而不是"看起来像成功"。
3. **驱动异常不越过仓储边界**：内存与实际仓储抛的都是领域异常，
   所以服务层不需要 ``import sqlalchemy``（这条由闸门 R6 兜底）。
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from packages.errors.error_codes import DomainError, ErrorRegistry
from packages.errors.repository_errors import (
    DuplicateEntityError,
    IntegrityViolationError,
    ReferenceViolationError,
)
from packages.shared.persistence import translate_integrity_error
from packages.shared.versioning import ConcurrentModificationError
from services.project_planning.domain.enums import ProjectTier
from services.project_planning.domain.project import Project
from services.project_planning.repository.in_memory_repo import (
    InMemoryProjectRepository,
)

_TENANT = "00000000-0000-0000-0000-000000000001"


class _FakeDriverError(Exception):
    """模拟驱动异常的 SQLSTATE 属性（asyncpg 风格）。"""

    def __init__(self, sqlstate: str) -> None:
        super().__init__(f"driver error {sqlstate}")
        self.sqlstate = sqlstate


def _integrity_error(sqlstate: str) -> IntegrityError:
    return IntegrityError("INSERT", {}, _FakeDriverError(sqlstate))


def test_unique_violation_maps_to_duplicate_entity() -> None:
    """唯一约束冲突 → ``DuplicateEntityError``（幂等语义的判定入口）。"""
    error = translate_integrity_error(
        _integrity_error("23505"), entity="project_id=p1"
    )
    assert isinstance(error, DuplicateEntityError)
    assert error.error_code is ErrorRegistry.IDEMPOTENCY_CONFLICT


def test_foreign_key_violation_maps_to_reference_violation() -> None:
    """外键悬空 → ``ReferenceViolationError``（调用方参数错误，不是内部错误）。"""
    error = translate_integrity_error(
        _integrity_error("23503"), entity="qc_report_id=q1"
    )
    assert isinstance(error, ReferenceViolationError)
    assert error.error_code is ErrorRegistry.VALIDATION_ERROR


def test_unknown_integrity_violation_is_internal_error() -> None:
    """未识别的完整性冲突（如 CHECK 违反）→ 内部错误。

    关键在"不静默"：约束冲突被当成成功时，数据不变量就失去了最后的观测点。
    """
    error = translate_integrity_error(
        _integrity_error("23514"), entity="asset_version_id=a1"
    )
    assert isinstance(error, IntegrityViolationError)
    assert error.error_code is ErrorRegistry.INTERNAL_ERROR


def test_duplicate_and_concurrency_are_finally_distinct() -> None:
    """两种冲突必须是不同的异常类型，且都带结构化错误码。

    这是本组测试的核心：此前它们都是 ``IntegrityError``，服务层只能靠
    "捕获后回查"猜是哪一种，猜错就把并发修改当成重复提交幂等返回了。
    """
    duplicate = translate_integrity_error(_integrity_error("23505"), entity="p1")
    concurrent = ConcurrentModificationError("p1", expected_version=3)

    assert type(duplicate) is not type(concurrent)
    assert isinstance(duplicate, DomainError)
    assert isinstance(concurrent, DomainError)
    assert duplicate.error_code is not concurrent.error_code


def test_translated_error_does_not_leak_column_values() -> None:
    """翻译后的详情只含实体标识，不含列值（避免把用户数据写进响应/日志）。"""
    error = translate_integrity_error(
        _integrity_error("23505"), entity="project_id=p1"
    )
    assert "project_id=p1" in (error.detail or "")
    assert "INSERT" not in (error.detail or "")


@pytest.mark.asyncio
async def test_in_memory_add_raises_domain_duplicate() -> None:
    """内存仓储抛领域异常（与翻译后的 SQL 仓储一致），不再 import 驱动。"""
    repo = InMemoryProjectRepository()
    project = Project(
        entity_id="p-dup",
        project_name="重复项目",
        project_tier=ProjectTier.B,
        created_by="tester",
        tenant_id=_TENANT,
        organization_id=_TENANT,
    )
    await repo.add(project)

    with pytest.raises(DuplicateEntityError):
        await repo.add(project)


@pytest.mark.asyncio
async def test_in_memory_save_raises_concurrency_conflict() -> None:
    """内存仓储的乐观锁失败是 ``ConcurrentModificationError``，不是"重复"。"""
    repo = InMemoryProjectRepository()
    project = Project(
        entity_id="p-lock",
        project_name="并发项目",
        project_tier=ProjectTier.B,
        created_by="tester",
        tenant_id=_TENANT,
        organization_id=_TENANT,
    )
    await repo.add(project)

    with pytest.raises(ConcurrentModificationError):
        await repo.save(project, expected_version=project.entity_version + 9)
