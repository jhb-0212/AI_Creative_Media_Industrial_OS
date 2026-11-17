"""ConstraintPackService 用例单元测试（内存 fake 仓储，无数据库依赖）。

覆盖（7.9 / ADR-088 / C3）：
- create_pack：一律落 DRAFT（Agent 也可创建）；NOT_APPLICABLE 语义校验；
- lock_pack：Agent 拒绝（不得自批自用）、跨项目拒绝、DRAFT→ACTIVE 固化
  审批人与版本、发出 CONSTRAINT_PACK_LOCKED 审计事件；
- supersede_pack：版本递增不覆盖（取代方版本必须更大）。
"""

from __future__ import annotations

import uuid
from uuid import uuid4

import pytest

from packages.errors.error_codes import DomainError
from packages.identity.principal import Principal, SubjectType
from packages.shared.outbox_writer import OutboxWriter
from services.shot_planning.domain.constraint_pack import ConstraintPack
from services.shot_planning.domain.enums import ConstraintPackApplicability
from services.shot_planning.repository.constraint_pack_repo import (
    ConstraintPackRepository,
)
from services.shot_planning.service.constraint_pack_service import (
    ConstraintPackService,
)

_TENANT = uuid.UUID("00000000-0000-0000-0000-000000000000")

_CONSTRAINT_ENTRY = {
    "constraint_id": "c_001",
    "target": "char_001",
    "shot_scope": "SHOT_001..SHOT_003",
    "rule": "服装/伤口状态连续",
    "source": "SCRIPT",
    "authority_level": "HARD",
    "valid_at": "2026-11-17T00:00:00Z",
    "approval_ref": "approval_001",
}


def _principal(
    subject_id: str = "director_001",
    subject_type: SubjectType = SubjectType.HUMAN,
) -> Principal:
    return Principal(
        subject_id=subject_id,
        tenant_id=_TENANT,
        organization_id=_TENANT,
        subject_type=subject_type,
        roles=frozenset({"导演"}),
        project_ids=frozenset({"*"}),
    )


class _RecordingOutboxWriter(OutboxWriter):
    def __init__(self) -> None:
        self.events: list[dict] = []

    def record(self, *, aggregate_type, aggregate_id, tenant_id, event_type, payload):  # type: ignore[override]
        self.events.append({"event_type": event_type, "payload": payload})
        return uuid4()


class _FakePackRepository(ConstraintPackRepository):
    def __init__(self) -> None:
        self._store: dict[str, ConstraintPack] = {}

    async def add(self, pack: ConstraintPack) -> None:
        self._store[pack.entity_id] = pack

    async def save(self, pack: ConstraintPack, expected_version: int) -> None:
        # 聚合根原地变更：save 收到的 expected_version 是读取时版本，
        # 变更后应恰好推进 1（乐观锁口径由真实仓储的条件 UPDATE 保证）。
        assert pack.entity_version == expected_version + 1, (
            "版本推进必须恰好 +1（乐观锁口径）"
        )
        self._store[pack.entity_id] = pack

    async def get_by_id(self, entity_id: str) -> ConstraintPack | None:
        return self._store.get(entity_id)


def _make_service() -> tuple[ConstraintPackService, _FakePackRepository, _RecordingOutboxWriter]:
    repo = _FakePackRepository()
    outbox = _RecordingOutboxWriter()
    return ConstraintPackService(repo, outbox), repo, outbox


async def test_agent_creates_draft_pack() -> None:
    """Agent 可创建约束包，但一律落 DRAFT（Agent 仅能创建 DRAFT）。"""
    service, repo, outbox = _make_service()
    pack = await service.create_pack(
        project_id="proj_001",
        shot_ids=["shot_001"],
        applicability=ConstraintPackApplicability.APPLICABLE,
        locked_dimensions=["camera_path"],
        constraints=[dict(_CONSTRAINT_ENTRY)],
        principal=_principal("agent_001", SubjectType.AGENT),
    )
    assert pack.status.value == "DRAFT"
    assert pack.approved_by is None
    assert repo._store[pack.entity_id] is pack
    assert outbox.events[0]["event_type"] == "CONSTRAINT_PACK_CREATED"


async def test_create_not_applicable_pack() -> None:
    """无锁定约束显式标 NOT_APPLICABLE，且不得携带锁定维度/约束（C3）。"""
    service, *_ = _make_service()
    pack = await service.create_pack(
        project_id="proj_001",
        shot_ids=["shot_001"],
        applicability=ConstraintPackApplicability.NOT_APPLICABLE,
        principal=_principal(),
    )
    assert pack.applicability is ConstraintPackApplicability.NOT_APPLICABLE
    assert pack.locked_dimensions == []
    assert pack.constraints == []


async def test_lock_pack_success() -> None:
    """授权人激活：DRAFT→ACTIVE，审批人与版本固定，发 CONSTRAINT_PACK_LOCKED。"""
    service, _, outbox = _make_service()
    pack = await service.create_pack(
        project_id="proj_001",
        shot_ids=["shot_001"],
        applicability=ConstraintPackApplicability.APPLICABLE,
        locked_dimensions=["camera_path"],
        constraints=[dict(_CONSTRAINT_ENTRY)],
        principal=_principal("agent_001", SubjectType.AGENT),
    )
    locked = await service.lock_pack(
        pack_id=pack.entity_id, project_id="proj_001", principal=_principal()
    )
    assert locked.status.value == "ACTIVE"
    assert locked.approved_by == "director_001"
    assert locked.approved_at is not None
    assert locked.entity_version == 2
    assert outbox.events[-1]["event_type"] == "CONSTRAINT_PACK_LOCKED"
    assert outbox.events[-1]["payload"]["approved_by"] == "director_001"


async def test_lock_pack_agent_denied() -> None:
    """Agent 激活约束包拒绝（「Agent 不得自批自用」红线）。"""
    service, _, _ = _make_service()
    pack = await service.create_pack(
        project_id="proj_001",
        shot_ids=["shot_001"],
        applicability=ConstraintPackApplicability.NOT_APPLICABLE,
        principal=_principal("agent_001", SubjectType.AGENT),
    )
    with pytest.raises(DomainError) as exc_info:
        await service.lock_pack(
            pack_id=pack.entity_id,
            project_id="proj_001",
            principal=_principal("agent_001", SubjectType.AGENT),
        )
    assert exc_info.value.error_code.code == "AUTH_SCOPE_DENIED"


async def test_lock_pack_cross_project_denied() -> None:
    """跨项目激活拒绝（防跨项目挂载）。"""
    service, _, _ = _make_service()
    pack = await service.create_pack(
        project_id="proj_001",
        shot_ids=["shot_001"],
        applicability=ConstraintPackApplicability.NOT_APPLICABLE,
        principal=_principal(),
    )
    with pytest.raises(DomainError) as exc_info:
        await service.lock_pack(
            pack_id=pack.entity_id,
            project_id="proj_other",
            principal=_principal(),
        )
    assert exc_info.value.error_code.code == "VALIDATION_ERROR"


async def test_supersede_requires_higher_version() -> None:
    """版本递增不覆盖：取代方版本必须大于被取代方（7.9）。"""
    service, repo, _ = _make_service()
    old_pack = await service.create_pack(
        project_id="proj_001",
        shot_ids=["shot_001"],
        applicability=ConstraintPackApplicability.NOT_APPLICABLE,
        principal=_principal(),
    )
    new_pack = await service.create_pack(
        project_id="proj_001",
        shot_ids=["shot_001"],
        applicability=ConstraintPackApplicability.NOT_APPLICABLE,
        version=2,
        principal=_principal(),
    )
    # 同版本取代拒绝。
    same_version = await service.create_pack(
        project_id="proj_001",
        shot_ids=["shot_001"],
        applicability=ConstraintPackApplicability.NOT_APPLICABLE,
        principal=_principal(),
    )
    with pytest.raises(DomainError) as exc_info:
        await service.supersede_pack(
            pack_id=old_pack.entity_id,
            superseded_by_pack_id=same_version.entity_id,
            principal=_principal(),
        )
    assert exc_info.value.error_code.code == "VALIDATION_ERROR"

    # 旧包必须先激活（7.9：SUPERSEDED 只能从 ACTIVE 进入）。
    await service.lock_pack(
        pack_id=old_pack.entity_id, project_id="proj_001", principal=_principal()
    )
    # 新版本激活后可取代旧包。
    await service.lock_pack(
        pack_id=new_pack.entity_id, project_id="proj_001", principal=_principal()
    )
    superseded = await service.supersede_pack(
        pack_id=old_pack.entity_id,
        superseded_by_pack_id=new_pack.entity_id,
        principal=_principal(),
    )
    assert superseded.status.value == "SUPERSEDED"
    assert superseded.entity_version == repo._store[old_pack.entity_id].entity_version
